from typing import Tuple
from functools import partial
import jax
import jax.numpy as jnp
from flax import optim
from flax.core.frozen_dict import FrozenDict
from haiku import PRNGSequence

from jax_rl.utils import double_mse
from jax_rl.utils import sample_from_multivariate_normal
from jax_rl.utils import apply_model
from jax_rl.utils import copy_params
from jax_rl.saving import save_model
from jax_rl.saving import load_model
from jax_rl.models import build_gaussian_policy_model
from jax_rl.models import build_double_critic_model
from jax_rl.models import build_constant_model


def actor_loss_fn(log_alpha: jnp.ndarray, log_p: jnp.ndarray, min_q: jnp.ndarray):
    return (jnp.exp(log_alpha) * log_p - min_q).mean()


def alpha_loss_fn(log_alpha: jnp.ndarray, target_entropy: float, log_p: jnp.ndarray):
    return (log_alpha * (-log_p - target_entropy)).mean()


@jax.jit
def get_td_target(
    rng: PRNGSequence,
    state: jnp.ndarray,
    action: jnp.ndarray,
    next_state: jnp.ndarray,
    reward: jnp.ndarray,
    not_done: jnp.ndarray,
    discount: float,
    max_action: float,
    actor_params: FrozenDict,
    critic_target_params: FrozenDict,
    log_alpha_params: FrozenDict,
) -> jnp.ndarray:
    next_action, next_log_p = apply_model(
        actor, actor_params, next_state, sample=True, key=rng
    )

    target_Q1, target_Q2 = apply_model(
        critic, critic_target_params, next_state, next_action
    )
    target_Q = (
        jnp.minimum(target_Q1, target_Q2)
        - jnp.exp(apply_model(log_alpha, log_alpha_params)) * next_log_p
    )
    target_Q = reward + not_done * discount * target_Q

    return target_Q


@jax.jit
def critic_step(
    optimizer: optim.Optimizer,
    state: jnp.ndarray,
    action: jnp.ndarray,
    target_Q: jnp.ndarray,
) -> optim.Optimizer:
    def loss_fn(critic_params):
        current_Q1, current_Q2 = apply_model(critic, critic_params, state, action)
        critic_loss = double_mse(current_Q1, current_Q2, target_Q)
        return jnp.mean(critic_loss)

    grad = jax.grad(loss_fn)(optimizer.target)
    return optimizer.apply_gradient(grad)


@jax.jit
def actor_step(
    rng: PRNGSequence,
    optimizer: optim.Optimizer,
    critic_params: FrozenDict,
    state: jnp.ndarray,
    log_alpha_params: FrozenDict,
) -> Tuple[optim.Optimizer, jnp.ndarray]:
    def loss_fn(actor_params):
        actor_action, log_p = apply_model(
            actor, actor_params, state, sample=True, key=rng
        )
        q1, q2 = apply_model(critic, critic_params, state, actor_action)
        min_q = jnp.minimum(q1, q2)
        partial_loss_fn = jax.vmap(
            partial(
                actor_loss_fn,
                jax.lax.stop_gradient(apply_model(log_alpha, log_alpha_params)),
            ),
        )
        actor_loss = partial_loss_fn(log_p, min_q)
        return jnp.mean(actor_loss), log_p

    grad, log_p = jax.grad(loss_fn, has_aux=True)(optimizer.target)
    return optimizer.apply_gradient(grad), log_p


@jax.jit
def alpha_step(
    optimizer: optim.Optimizer, log_p: jnp.ndarray, target_entropy: float
) -> optim.Optimizer:
    log_p = jax.lax.stop_gradient(log_p)

    def loss_fn(log_alpha_params):
        partial_loss_fn = jax.vmap(
            partial(
                alpha_loss_fn, apply_model(log_alpha, log_alpha_params), target_entropy
            )
        )
        return jnp.mean(partial_loss_fn(log_p))

    grad = jax.grad(loss_fn)(optimizer.target)
    return optimizer.apply_gradient(grad)


class SAC:
    def __init__(
        self,
        state_dim: int,
        action_dim: int,
        max_action: float,
        discount: float = 0.99,
        tau: float = 0.005,
        policy_freq: int = 2,
        lr: float = 3e-4,
        entropy_tune: bool = True,
        seed: int = 0,
    ):
        self.rng = PRNGSequence(seed)

        actor_input_dim = (1, state_dim)

        # TODO: has to be a cleaner way to do this
        global actor
        actor, actor_params = build_gaussian_policy_model(
            actor_input_dim, action_dim, max_action, next(self.rng)
        )
        actor_optimizer = optim.Adam(learning_rate=lr).create(actor_params)
        self.actor_optimizer = jax.device_put(actor_optimizer)

        init_rng = next(self.rng)

        critic_input_dim = [(1, state_dim), (1, action_dim)]

        # TODO: has to be a cleaner way to do this
        global critic
        critic, critic_params = build_double_critic_model(critic_input_dim, init_rng)
        _, self.critic_target_params = build_double_critic_model(
            critic_input_dim, init_rng
        )
        critic_optimizer = optim.Adam(learning_rate=lr).create(critic_params)
        self.critic_optimizer = jax.device_put(critic_optimizer)

        self.entropy_tune = entropy_tune

        # TODO: has to be a cleaner way to do this
        global log_alpha
        log_alpha, log_alpha_params = build_constant_model(-3.5, next(self.rng))
        log_alpha_optimizer = optim.Adam(learning_rate=lr).create(log_alpha_params)
        self.log_alpha_optimizer = jax.device_put(log_alpha_optimizer)
        self.target_entropy = -action_dim

        self.max_action = max_action
        self.discount = discount
        self.tau = tau
        self.policy_freq = policy_freq

        self.total_it = 0

    @property
    def target_params(self):
        return (
            self.discount,
            self.max_action,
            self.actor_optimizer.target,
            self.critic_target_params,
            self.log_alpha_optimizer.target,
        )

    def select_action(self, state: jnp.ndarray) -> jnp.ndarray:
        mu, _ = apply_model(actor, self.actor_optimizer.target, state)
        return mu.flatten()

    def sample_action(self, rng: PRNGSequence, state: jnp.ndarray) -> jnp.ndarray:
        mu, log_sig = apply_model(actor, self.actor_optimizer.target, state)
        return mu + random.normal(rng, mu.shape) * jnp.exp(log_sig)

    def train(self, replay_buffer, batch_size=100):
        self.total_it += 1

        buffer_out = replay_buffer.sample(next(self.rng), batch_size)

        target_Q = jax.lax.stop_gradient(
            get_td_target(next(self.rng), *buffer_out, *self.target_params)
        )

        state, action, *_ = buffer_out

        self.critic_optimizer = critic_step(
            self.critic_optimizer, state, action, target_Q
        )

        if self.total_it % self.policy_freq == 0:

            self.actor_optimizer, log_p = actor_step(
                next(self.rng),
                self.actor_optimizer,
                self.critic_optimizer.target,
                state,
                self.log_alpha_optimizer.target,
            )

            if self.entropy_tune:
                self.log_alpha_optimizer = alpha_step(
                    self.log_alpha_optimizer, log_p, self.target_entropy
                )

            self.critic_target_params = copy_params(
                self.critic_optimizer.target, self.critic_target_params, self.tau
            )

    def save(self, filename):
        save_model(filename + "_critic", self.critic_optimizer)
        save_model(filename + "_actor", self.actor_optimizer)

    def load(self, filename):
        self.critic_optimizer = load_model(filename + "_critic", self.critic_optimizer)
        self.critic_optimizer = jax.device_put(self.critic_optimizer)
        self.critic_target_params = self.critic_optimizer.target.params.copy()

        self.actor_optimizer = load_model(filename + "_actor", self.actor_optimizer)
        self.actor_optimizer = jax.device_put(self.actor_optimizer)
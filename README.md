# jax-rl

Core Deep Reinforcement Learning algorithms using JAX for improved performance relative to PyTorch and TensorFlow. Control tasks rely on the [DeepMind Control Suite](https://github.com/deepmind/dm_control); see repo for further setup if you don't have MuJoCo configured.

## Current implementations

- [x] TD3
- [x] SAC
- [x] MPO

## Environment and Testing

This repo makes use of the `poetry` package and dependency management tool. To build a local environment with all necessary packages run:

```bash
make install
```

To test local changes run:

```bash
make test
```

# Run

To run each algorithm on cartpole swingup from the base directory:

```bash
python jax_rl/main_dm_control.py --policy TD3 --max_timestep 100000
python jax_rl/main_dm_control.py --policy SAC --max_timesteps 100000
python jax_rl/main_dm_control.py --policy MPO --max_timesteps 100000
```

# Results

Better benchmarking to come on a wider range of environments and with more seeds. Below is the most simple example to demonstrate that the algorithms converge well on a simple task.

![](docs/_static/reward_plots.png?raw=true)
*Evaluation of deterministic policy (acting according to the mean for SAC and MPO) every 1000 training steps for each algorithm. Important parameters are constant for all, including batch size of 256 per training step, 10000 samples to the replay buffer with uniform random sampling before training, and 100000 total steps in the environment.*

## Notes on MPO Implementation

Because we have direct access to the jacobian function with JAX, I've opted to use `scipy.optimize.minimize` instead of taking a single gradient step on the temperature parameter per iteration. In my testing this gives much greater stability with only a marginal increase in time per iteration. If your top priority is speed, this can be easily modified.

One important aspect to note if you are benchmarking these two approaches is that a standard profiler will be misleading. Most of the time will show up in the call to `scipy.optimize.minimize`, but this is due to how JAX calls work internally. JAX does not wait for an operation to complete when an operation is called, but rather returns a pointer to a `DeviceArray` whose value will be updated when the dispatched call is complete. If this object is passed into another JAX method, the same process will be repeated and control will be returned to Python. Any time Python attempts to access the value of a `DeviceArray` it will need to wait for the computation to complete. Because `scipy.optimize.minimize` passed the values of the parameter and the gradient to FORTRAN, this step will require the whole program to wait for all previous JAX calls to complete. To get a more accurate comparison, compare the total time per training step. To readmore about how asyncronous dispatch works in JAX, see [this reference](https://jax.readthedocs.io/en/latest/async_dispatch.html).

from collections import deque, namedtuple
import itertools
import os
import math
import random

from moviepy.editor import ImageSequenceClip
import numpy as np
import torch
from torch.distributions import constraints
from torch.distributions.transforms import Transform
from torch.nn.functional import softplus

Transition = namedtuple('Transition', ('state', 'action', 'reward', 'nextstate', 'real_done'))


class MeanStdevFilter():
    def __init__(self, shape, clip=3.0):
        self.eps = 1e-4
        self.shape = shape
        self.clip = clip
        self._count = 0
        self._running_sum = np.zeros(shape)
        self._running_sum_sq = np.zeros(shape) + self.eps
        self.mean = np.zeros(shape)
        self.stdev = np.ones(shape) * self.eps

    def update(self, x):
        if len(x.shape) == 1:
            x = x.reshape(1,-1)
        self._running_sum += np.sum(x, axis=0)
        self._running_sum_sq += np.sum(np.square(x), axis=0)
        # assume 2D data
        self._count += x.shape[0]
        self.mean = self._running_sum / self._count
        self.stdev = np.sqrt(
            np.maximum(
                self._running_sum_sq / self._count - self.mean**2,
                 self.eps
                 ))
    
    def __call__(self, x):
        return np.clip(((x - self.mean) / self.stdev), -self.clip, self.clip)

    def invert(self, x):
        return (x * self.stdev) + self.mean


class ReplayPool:

    def __init__(self, capacity=1e6):
        self.capacity = int(capacity)
        self._memory = deque(maxlen=int(capacity))
        
    def push(self, transition: Transition):
        """ Saves a transition """
        self._memory.append(transition)
        
    def sample(self, batch_size: int, unique: bool = True, dist=None) -> Transition:
        transitions = random.sample(self._memory, batch_size) if unique else random.choices(self._memory, k=batch_size)
        return Transition(*zip(*transitions))

    def get(self, start_idx: int, end_idx: int) -> Transition:
        transitions = list(itertools.islice(self._memory, start_idx, end_idx))
        return transitions

    def get_all(self) -> Transition:
        return self.get(0, len(self._memory))

    def __len__(self) -> int:
        return len(self._memory)

    def clear_pool(self):
        self._memory.clear()

    def initialise(self, old_pool: 'ReplayPool'):
        old_memory = old_pool.get_all()
        self._memory.extend(old_memory)


# Code courtesy of JPH: https://github.com/jparkerholder
def make_gif(policy, env, step_count, state_filter, maxsteps=1000):
    envname = env.spec.id
    gif_name = '_'.join([envname, str(step_count)])
    state = env.reset()
    done = False
    steps = []
    rewards = []
    t = 0
    while (not done) & (t< maxsteps):
        s = env.render('rgb_array')
        steps.append(s)
        action = policy.get_action(state, state_filter=state_filter, deterministic=True)
        action = np.clip(action, env.action_space.low[0], env.action_space.high[0])
        action = action.reshape(len(action), )
        state, reward, done, _ = env.step(action)
        rewards.append(reward)
        t +=1
    print('Final reward :', np.sum(rewards))
    clip = ImageSequenceClip(steps, fps=30)
    if not os.path.isdir('gifs'):
        os.makedirs('gifs')
    clip.write_gif('gifs/{}.gif'.format(gif_name), fps=30)


def make_checkpoint(agent, step_count, env_name, save_replay_pool=False):

    save_dir = "checkpoints/{}".format(env_name)

    save_path = save_dir + "/{}-{}steps-seed{}.pt".format(agent.alg_name, step_count, agent._seed)
   
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)

    save_dict = {
        'double_q_state_dict': agent.q_funcs.state_dict(),
        'target_double_q_state_dict': agent.target_q_funcs.state_dict(),
        'policy_state_dict': agent.policy.state_dict(),
        'replay_pool': agent.replay_pool if save_replay_pool else None,
        'num_updates': agent._update_counter,
        'num_steps': step_count,
        'alg_name': agent.alg_name,
        'env_name': env_name
        }

    if agent.is_soft:
        save_dict['log_alpha'] = agent._log_alpha

    if hasattr(agent, "target_policy"):
        save_dict['target_policy_state_dict'] = agent.target_policy.state_dict()

    print("Saving {} Policy at {} Steps".format(agent.alg_name, step_count))
    torch.save(save_dict, save_path)


# Taken from: https://github.com/pytorch/pytorch/pull/19785/files
# The composition of affine + sigmoid + affine transforms is numerically unstable
# tanh transform is (2 * sigmoid(2x) - 1)
# Old Code Below:
# transforms = [AffineTransform(loc=0, scale=2), SigmoidTransform(), AffineTransform(loc=-1, scale=2)]
class TanhTransform(Transform):
    r"""
    Transform via the mapping :math:`y = \tanh(x)`.
    It is equivalent to
    ```
    ComposeTransform([AffineTransform(0., 2.), SigmoidTransform(), AffineTransform(-1., 2.)])
    ```
    However this might not be numerically stable, thus it is recommended to use `TanhTransform`
    instead.
    Note that one should use `cache_size=1` when it comes to `NaN/Inf` values.
    """
    domain = constraints.real
    codomain = constraints.interval(-1.0, 1.0)
    bijective = True
    sign = +1

    @staticmethod
    def atanh(x):
        return 0.5 * (x.log1p() - (-x).log1p())

    def __eq__(self, other):
        return isinstance(other, TanhTransform)

    def _call(self, x):
        return x.tanh()

    def _inverse(self, y):
        # We do not clamp to the boundary here as it may degrade the performance of certain algorithms.
        # one should use `cache_size=1` instead
        return self.atanh(y)

    def log_abs_det_jacobian(self, x, y):
        # We use a formula that is more numerically stable, see details in the following link
        # https://github.com/tensorflow/probability/blob/master/tensorflow_probability/python/bijectors/tanh.py#L69-L80
        return 2. * (math.log(2.) - x - softplus(-2. * x))
from collections import namedtuple
from typing import Any
from typing import Sequence
from typing import Tuple
from typing import Union

import numpy as np
import torch

_DATA = Union[np.ndarray, torch.Tensor]

Batch = namedtuple('Batch', 'state action reward next_state done')


class RingBuffer:
    TORCH_BACKEND = False

    def __init__(self, capacity: int, dimension: Union[int, Sequence[int]]):
        self._size = 0
        self._capacity = capacity
        self._dimension = self._to_tuple(dimension)
        self._container = np.zeros((capacity,) + self._dimension)

    def add(self, value: Any):
        if self._size < self._capacity:
            self._container[self._size, :] = value
            self._size += 1
        elif self._size == self._capacity:
            self._container = np.roll(self._container, -1, 0)
            self._container[self._capacity - 1, :] = value
        else:
            raise ValueError

    def sample(self,
               idx: Union[Sequence[int], torch.Tensor],
               device: torch.device = torch.device('cpu')) -> _DATA:
        batch = self._container[idx]
        if RingBuffer.TORCH_BACKEND:
            return torch.from_numpy(batch).float().to(device)
        return batch

    def reset(self):
        self._container = np.zeros((self._capacity,) + self._dimension)

    @property
    def size(self) -> int:
        return self._size

    @property
    def first(self) -> _DATA:
        return self._container[0]

    @property
    def end(self) -> _DATA:
        return self._container[-1]

    @property
    def data(self) -> _DATA:
        return self._container

    @staticmethod
    def _to_tuple(value: Union[int, Sequence[int]]) -> Tuple[int, ...]:
        if isinstance(value, int):
            return value,
        return tuple(value)

    def __str__(self) -> str:
        return np.array2string(self._container)

    def __getitem__(self, item: int) -> np.ndarray:
        return self._container[item]


class ReplayMemory(object):
    def __init__(self,
                 capacity: int,
                 state_dim: Union[int, Sequence[int]],
                 action_dim: Union[int, Sequence[int]],
                 combined: bool = False,
                 torch_backend: bool = False):
        RingBuffer.TORCH_BACKEND = torch_backend
        self._capacity = capacity
        self._combined = combined

        self._state_buffer = RingBuffer(capacity, state_dim)
        self._action_buffer = RingBuffer(capacity, action_dim)
        self._reward_buffer = RingBuffer(capacity, 1)
        self._next_state_buffer = RingBuffer(capacity, state_dim)
        self._terminal_buffer = RingBuffer(capacity, 1)

    def push(self,
             state: _DATA,
             action: _DATA,
             reward: Union[float, torch.Tensor],
             next_state: Union[float, torch.Tensor],
             terminal: Union[float, bool, torch.Tensor]):
        self._state_buffer.add(state)
        self._action_buffer.add(action)
        self._reward_buffer.add(reward)
        self._next_state_buffer.add(next_state)
        self._terminal_buffer.add(terminal)

    def sample(self,
               batch_size: int,
               device: torch.device = torch.device('cpu')) -> Batch:
        if self._combined:
            batch_size -= 1
        idxs = np.random.randint((self.size - 1), size=batch_size)
        if self._combined:
            idxs = np.append(idxs, np.array(self.size - 1, dtype=np.int32))
        batch = Batch(state=self._state_buffer.sample(idxs, device),
                      action=self._action_buffer.sample(idxs, device),
                      reward=self._reward_buffer.sample(idxs, device),
                      next_state=self._next_state_buffer.sample(idxs, device),
                      done=self._terminal_buffer.sample(idxs, device))

        return batch

    @property
    def size(self) -> int:
        return self._state_buffer.size

    def __getitem__(self, item: int) -> Tuple[np.ndarray, ...]:
        state = self._state_buffer[item]
        action = self._action_buffer[item]
        reward = self._reward_buffer[item]
        next_state = self._next_state_buffer[item]
        terminal = self._terminal_buffer[item]
        return state, action, reward, next_state, terminal


class Rollout:
    def __init__(self,
                 capacity: int,
                 state_dim: int,
                 action_dim: int,
                 discount_factor: float):
        self._size = 0
        self._capacity = capacity
        self._discount_factor = discount_factor

        self._state_buffer = RingBuffer(capacity, state_dim)
        self._action_buffer = RingBuffer(capacity, action_dim)
        self._reward_buffer = RingBuffer(capacity, 1)

    @property
    def ready(self):
        return self._size == self._capacity

    def push(self,
             state: _DATA,
             action: _DATA,
             reward: Union[float, torch.Tensor]):
        self._state_buffer.add(state)
        self._action_buffer.add(action)
        self._reward_buffer.add(reward)
        self._size = min(self._size + 1, self._capacity)

    def get_transition(self, state, action, reward, next_state, done):
        self.push(state, action, reward)
        if self._size == self._capacity:
            cum_reward = self._compute_cumulative_reward()
            state = self._state_buffer[0]
            action = self._action_buffer[0]
            return state, action, cum_reward, next_state, done
        return None

    def reset(self):
        self._state_buffer.reset()
        self._action_buffer.reset()
        self._reward_buffer.reset()

    def _compute_cumulative_reward(self):
        cum_reward = 0
        for t in range(self._capacity):
            cum_reward += self._discount_factor ** t * self._reward_buffer[t]
        return cum_reward

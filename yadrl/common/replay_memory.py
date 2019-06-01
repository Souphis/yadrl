from typing import *
from collections import namedtuple

import numpy as np
import torch

_DATA = Union[np.ndarray, torch.Tensor]

Batch = namedtuple('Batch', 'state action reward next_state done')


class RingBuffer(object):
    TORCH_BACKEND = False

    def __init__(self, capacity: int, dimension: Union[int, Sequence[int]]):
        self._backend = torch if RingBuffer.TORCH_BACKEND else np
        self._size = 0
        self._capacity = capacity
        self._container = self._backend.zeros(self._to_tuple(capacity) + self._to_tuple(dimension))

    def add(self, value: Any) -> NoReturn:
        if RingBuffer.TORCH_BACKEND:
            value = self._to_torch(value)

        if self._size < self._capacity:
            self._container[self._size, :] = value
            self._size += 1
        elif self._size == self._capacity:
            self._container = self._backend.roll(self._container, -1, 0)
            self._container[self._capacity - 1, :] = value
        else:
            raise ValueError

    def sample(self,
               idx: Union[Sequence[int], torch.Tensor],
               device: torch.device = torch.device('cpu')) -> _DATA:
        batch = self._container[self._to_torch(idx).long(), :]
        if RingBuffer.TORCH_BACKEND:
            return batch.to(device)
        return batch

    @property
    def size(self) -> int:
        return self._size

    @property
    def first(self) -> _DATA:
        return self._container[0]

    @property
    def end(self) -> _DATA:
        return self._container[-1]

    @staticmethod
    def _to_tuple(value: Union[int, Sequence[int]]) -> Tuple[int, ...]:
        if isinstance(value, int):
            return value,
        return tuple(value)

    @staticmethod
    def _to_torch(value: Any, reshape: bool = False) -> torch.Tensor:
        if not isinstance(value, np.ndarray):
            value = np.asarray(value)
        if reshape:
            return torch.from_numpy(value).float().view(-1, 1)
        return torch.from_numpy(value).float()


class ReplayMemory(object):
    def __init__(self,
                 capacity: int,
                 state_dim: Union[int, Sequence[int]],
                 action_dim: Union[int, Sequence[int]],
                 torch_backend: bool = False):
        RingBuffer.TORCH_BACKEND = torch_backend
        self._capacity = capacity

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
        idxs = np.random.randint((self.size - 1), size=batch_size)

        batch = Batch(state=self._state_buffer.sample(idxs, device),
                      action=self._action_buffer.sample(idxs, device),
                      reward=self._reward_buffer.sample(idxs, device),
                      next_state=self._next_state_buffer.sample(idxs, device),
                      done=self._terminal_buffer.sample(idxs, device))

        return batch

    @property
    def size(self) -> int:
        return self._state_buffer.size

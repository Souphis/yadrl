import random
from copy import deepcopy
from typing import Any, Union

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from .base import BaseOffPolicy
from yadrl.common.heads import DQNHead, DuelingDQNHead


class DQN(BaseOffPolicy):
    def __init__(self,
                 phi: nn.Module,
                 lrate: float,
                 epsilon_decay_factor: float,
                 epsilon_min: float,
                 use_soft_update: bool = True,
                 use_double_q: bool = False,
                 use_dueling: bool = False, **kwargs):
        super(DQN, self).__init__(**kwargs)
        self._use_double_q = use_double_q
        self._use_soft_update = use_soft_update

        self._eps = 1.0
        self._esp_decay_factor = epsilon_decay_factor
        self._eps_min = epsilon_min

        head = DuelingDQNHead if use_dueling else DQNHead
        self._model = head(phi, self._action_dim)
        self._model.to(self._device)

        self._target_model = deepcopy(self._model).to(self._device)
        self._target_model.eval()

        self._optim = optim.Adam(self._model.parameters(), lr=lrate)

    def act(self, state: np.ndarray, train: bool = False) -> np.ndarray:
        self._eps = max(self._eps * self._esp_decay_factor, self._eps_min)
        state = torch.from_numpy(state).float().to(self._device)

        self._model.eval()
        with torch.no_grad():
            action = torch.argmax(self._model(state))

        if random.random() > self._eps or not train:
            return action.cpu().numpy()
        return np.array([random.randint(0, self._action_dim)])

    def observe(self,
                state: Union[np.ndarray, torch.Tensor],
                action: Union[np.ndarray, torch.Tensor],
                reward: Union[float, torch.Tensor],
                next_state: Union[np.ndarray, torch.Tensor],
                done: Any):
        self._memory.push(state, action, reward, next_state, done)
        if self._memory.size > self._warm_up_steps:
            self._step += 1
            self.update()

    def update(self):
        batch = self._memory.sample(self._batch_size, self._device)

        self._model.train()

        if self._use_double_q:
            next_action = self._model(batch.next_state).argmax(1).view(-1, 1)
            target_next_q = self._target_model(batch.next_state).gather(1, next_action).detach()
        else:
            target_next_q = self._target_model(batch.next_state).max(1)[0].view(-1, 1).detach()

        target_q = batch.reward + (1.0 - batch.done) * self._discount * target_next_q
        expected_q = self._model(batch.state).gather(1, batch.action)
        loss = torch.mean(0.5 * (expected_q - target_q) ** 2)

        self._optim.zero_grad()
        loss.backward()
        self._optim.step()

        if self._use_soft_update:
            self._soft_update(self._model.parameters(), self._target_model.parameters())

        if not self._use_soft_update and self._step % self._polyak == 0:
            self._hard_update(self._model, self._target_model)

import os
import random
from copy import deepcopy
from typing import NoReturn

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from .base import BaseOffPolicy
from yadrl.common.heads import DQNHead, DuelingDQNHead


class DQN(BaseOffPolicy):
    def __init__(self,
                 phi: nn.Module,
                 action_dim: int,
                 lrate: float,
                 epsilon_decay_factor: float,
                 epsilon_min: float,
                 use_soft_update: bool = True,
                 use_double_q: bool = False,
                 use_dueling: bool = False, **kwargs):
        super(DQN, self).__init__(action_dim=1, **kwargs)
        self._action_dim = action_dim

        self._use_double_q = use_double_q
        self._use_soft_update = use_soft_update

        self._eps = 1.0
        self._esp_decay_factor = epsilon_decay_factor
        self._eps_min = epsilon_min

        head = DuelingDQNHead if use_dueling else DQNHead
        self._model = head(phi, self._action_dim).to(self._device)
        self._target_model = head(phi, self._action_dim).to(self._device)

        self.load()
        self._target_model.load_state_dict(self._model.state_dict())

        self._optim = optim.Adam(self._model.parameters(), lr=lrate)

    def act(self, state: int, train: bool = False) -> np.ndarray:
        self._eps = max(self._eps * self._esp_decay_factor, self._eps_min)
        state = torch.from_numpy(state).float().to(self._device)

        self._model.eval()
        with torch.no_grad():
            action = torch.argmax(self._model(state))
        self._model.train()

        if random.random() > self._eps or not train:
            return action.cpu().numpy()
        return random.randint(0, self._action_dim - 1)

    def update(self):
        batch = self._memory.sample(self._batch_size, self._device)
        mask = 1.0 - batch.done
        if self._use_double_q:
            next_action = self._model(batch.next_state).argmax(1).view(-1, 1)
            target_next_q = self._target_model(batch.next_state).gather(1, next_action)
        else:
            target_next_q = self._target_model(batch.next_state).max(1)[0].view(-1, 1)

        target_q = batch.reward + mask * self._discount * target_next_q.detach()
        expected_q = self._model(batch.state).gather(1, batch.action.long())
        loss = self._mse_loss(expected_q, target_q)

        self._optim.zero_grad()
        loss.backward()
        self._optim.step()

        if self._use_soft_update:
            self._soft_update(self._model.parameters(), self._target_model.parameters())

        if not self._use_soft_update and self.step % self._polyak == 0:
            self._hard_update(self._model, self._target_model)

    def load(self) -> NoReturn:
        if os.path.isfile(self._checkpoint):
            self._model.load_state_dict(torch.load(self._checkpoint))
            print('Model found and loaded!')
            return
        if not os.path.isdir(os.path.split(self._checkpoint)[0]):
            os.mkdir(os.path.split(self._checkpoint)[0])
        print('Model not found!')

    def save(self):
        torch.save(self._model.state_dict(), self._checkpoint)

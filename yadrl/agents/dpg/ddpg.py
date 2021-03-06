from copy import deepcopy
from itertools import chain
from typing import Sequence

import numpy as np
import torch as th
import torch.nn as nn
import torch.optim as optim

import yadrl.common.ops as ops
import yadrl.common.types as t
from yadrl.agents.agent import OffPolicyAgent
from yadrl.common.exploration_noise import GaussianNoise
from yadrl.common.memory import Batch
from yadrl.networks.head import Head


class DDPG(OffPolicyAgent, agent_type="ddpg"):
    def __init__(
        self,
        pi_learning_rate: float,
        qv_learning_rate: float,
        action_limit: Sequence[float],
        exploration_strategy: GaussianNoise,
        noise_scale_factor: float = 1.0,
        l2_lambda: float = 0.01,
        pi_grad_norm_value: float = 0.0,
        qv_grad_norm_value: float = 0.0,
        policy_update_frequency: int = 1,
        **kwargs
    ):
        super().__init__(**kwargs)
        assert np.shape(action_limit) == (2,), "Wrong action limit!"

        self._action_limit = action_limit
        self._policy_update_frequency = policy_update_frequency
        self._pi_grad_norm_value = pi_grad_norm_value
        self._qv_grad_norm_value = qv_grad_norm_value
        self._l2_lambda = l2_lambda

        self._pi_optim = optim.Adam(self.pi.parameters(), pi_learning_rate)
        self._qv_optim = optim.Adam(self.qv.parameters(), qv_learning_rate)

        self._noise = exploration_strategy
        self._noise_scale_factor = noise_scale_factor

    @property
    def pi(self) -> nn.Module:
        return self._networks["actor"]

    @property
    def qv(self) -> nn.Module:
        return self._networks["critic"]

    @property
    def target_pi(self) -> nn.Module:
        return self._networks["target_actor"]

    @property
    def target_qv(self) -> nn.Module:
        return self._networks["target_critic"]

    def _initialize_networks(self, phi: t.TModuleDict) -> t.TModuleDict:
        support_dim = self._support_dim if hasattr(self, "_support_dim") else 1
        actor_net = Head.build(
            head_type="simple",
            phi=phi["actor"],
            output_dim=self._action_dim,
            output_activation="tanh",
        )
        critic_net = Head.build(
            head_type="simple", phi=phi["critic"], output_dim=support_dim
        )
        target_actor_net = deepcopy(actor_net)
        target_critic_net = deepcopy(critic_net)

        actor_net.to(self._device)
        critic_net.to(self._device)
        target_actor_net.to(self._device)
        target_critic_net.to(self._device)
        target_actor_net.eval()
        target_critic_net.eval()

        return {
            "actor": actor_net,
            "critic": critic_net,
            "target_actor": target_actor_net,
            "target_critic": target_critic_net,
        }

    def _act(self, state: np.ndarray, train: bool = False) -> np.ndarray:
        self.pi.eval()
        with th.no_grad():
            action = self.pi(super()._act(state))
        self.pi.train()
        if train:
            noise = self._noise_scale_factor * self._noise().to(self._device)
            action = th.clamp(action + noise, *self._action_limit)
        return action[0].cpu().numpy()

    def _observe(
        self,
        state: t.TData,
        action: t.TActionOption,
        reward: float,
        next_state: t.TData,
        done: bool,
    ):
        super()._observe(state, action, reward, next_state, done)
        if done:
            self._noise.reset()

    def _sample_q(
        self, state: th.Tensor, action: th.Tensor, sample_noise: bool = False
    ) -> th.Tensor:
        self.qv.reset_noise()
        if sample_noise:
            self.qv.sample_noise()
        return self.qv(state, action)

    def _update(self):
        batch = self._memory.sample(self._batch_size, self._state_normalizer)
        self._update_critic(batch)
        update_freq = self._policy_update_frequency * self._update_frequency
        if self._env_step % update_freq == 0:
            self._update_actor(batch)
            self._update_target(self.pi, self.target_pi)
            self._update_target(self.qv, self.target_qv)

    def _update_critic(self, batch: Batch):
        loss = self._compute_critic_loss(batch)
        if self._l2_lambda > 0.0:
            loss += ops.l2_loss(self.qv, self._l2_lambda, self._device)

        self._qv_optim.zero_grad()
        loss.backward()
        if self._qv_grad_norm_value > 0.0:
            nn.utils.clip_grad_norm_(self.qv.parameters(), self._qv_grad_norm_value)
        self._qv_optim.step()
        self._writer.add_scalar("train/loss/qv", loss.item(), self._env_step)

    def _compute_critic_loss(self, batch: Batch) -> th.Tensor:
        with th.no_grad():
            next_action = self.target_pi(batch.next_state)
            self.target_qv.sample_noise()
            target_next_q = self.target_qv(batch.next_state, next_action)
            target_q = ops.td_target(
                batch.reward, batch.mask, target_next_q, self._discount
            )

        self.qv.sample_noise()
        expected_q = self.qv(batch.state, batch.action)
        return ops.mse_loss(expected_q, target_q)

    def _update_actor(self, batch: Batch):
        state = self._state_normalizer(batch.state, self._device)
        loss = -self._sample_q(state, self.pi(state), True).mean()
        self._pi_optim.zero_grad()

        loss.backward()
        if self._pi_grad_norm_value > 0.0:
            nn.utils.clip_grad_norm_(self.pi.parameters(), self._pi_grad_norm_value)
        self._pi_optim.step()
        self._writer.add_scalar("train/loss/pi", loss.item(), self._env_step)

    @property
    def parameters(self) -> t.TNamedParameters:
        return chain(self.qv.named_parameters(), self.pi.named_parameters())

    @property
    def target_parameters(self) -> t.TNamedParameters:
        return chain(
            self.target_qv.named_parameters(), self.target_pi.named_parameters()
        )

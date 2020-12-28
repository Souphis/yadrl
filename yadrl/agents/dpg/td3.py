from copy import deepcopy

import torch

import yadrl.common.ops as ops
from yadrl.agents.dpg.ddpg import DDPG
from yadrl.common.exploration_noise import GaussianNoise
from yadrl.common.memory import Batch
from yadrl.networks.head import Head


class TD3(DDPG, agent_type='td3'):
    def __init__(self,
                 target_noise_limit: float = 0.5,
                 target_noise_std: float = 0.2,
                 **kwargs):
        super().__init__(**kwargs)
        self._target_noise_limit = (-target_noise_limit, target_noise_limit)
        self._target_noise = GaussianNoise(
            self._action_dim, sigma=target_noise_std, n_step_annealing=0)

    def _initialize_networks(self, phi):
        networks = super()._initialize_networks(phi)
        critic_net = Head.build(head_type='multi', phi=phi['critic'],
                                output_dim=1, num_heads=2)
        target_critic_net = deepcopy(critic_net)
        critic_net.to(self._device)
        target_critic_net.to(self._device)
        target_critic_net.eval()
        networks['critic'] = critic_net
        networks['target_critic'] = target_critic_net

        return networks

    def _sample_q(self,
                  state: torch.Tensor,
                  action: torch.Tensor,
                  sample_noise: bool = False) -> torch.Tensor:
        self.qv.reset_noise()
        if sample_noise:
            self.qv.sample_noise()
        return self.qv.evaluate_head(state, action, idx=0)

    def _compute_critic_loss(self, batch: Batch) -> torch.Tensor:
        state = self._state_normalizer(batch.state, self._device)
        next_state = self._state_normalizer(batch.next_state, self._device)

        with torch.no_grad():
            noise = self._target_noise().clamp(
                *self._target_noise_limit).to(self._device)
            next_action = self.target_pi(next_state) + noise
            next_action = next_action.clamp(*self._action_limit)

            self.target_qv.sample_noise()
            target_next_qs = self.target_qv(next_state, next_action)
            target_next_q = torch.min(*target_next_qs).view(-1, 1)
            target_q = ops.td_target(batch.reward, batch.mask, target_next_q,
                                     batch.discount_factor * self._discount)

        self.qv.sample_noise()
        expected_qs = self.qv(state, batch.action)
        return sum([ops.mse_loss(q, target_q) for q in expected_qs])

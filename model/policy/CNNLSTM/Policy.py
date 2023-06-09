import torch
import torch.nn as nn
from model.rnn_state_encoder import RNNStateEncoder
from custom_habitat_baselines.common.utils import CategoricalNet
from model.resnet import resnet
from model.resnet.resnet import ResNetEncoder
from .perception import Perception

class CriticHead(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.fc = nn.Linear(input_size, 1)
        nn.init.orthogonal_(self.fc.weight)
        nn.init.constant_(self.fc.bias, 0)

    def forward(self, x):
        return self.fc(x)

class CNNLSTMPolicy(nn.Module):
    def __init__(
            self,
            observation_space, # a SpaceDict instace. See line 35 in train_bc.py
            action_space,
            goal_sensor_uuid="pointgoal_with_gps_compass",
            hidden_size=512,
            num_recurrent_layers=2,
            rnn_type="LSTM",
            resnet_baseplanes=32,
            backbone="resnet50",
            normalize_visual_inputs=True,
            cfg=None
    ):
        super().__init__()
        self.net = CNNLSTMNet(
            observation_space=observation_space,
            action_space=action_space,
            goal_sensor_uuid=goal_sensor_uuid,
            hidden_size=hidden_size,
            num_recurrent_layers=num_recurrent_layers,
            rnn_type=rnn_type,
            backbone=backbone,
            resnet_baseplanes=resnet_baseplanes,
            normalize_visual_inputs=normalize_visual_inputs,
            cfg=cfg
        )
        self.dim_actions = action_space.n # action_space = Discrete(config.ACTION_DIM)

        self.action_distribution = CategoricalNet(
            self.net.output_size, self.dim_actions
        )
        self.critic = CriticHead(self.net.output_size) # a single layer FC

    def act(
            self,
            observations, # obs are generated by calling env_wrapper.step() in line 59, bc_trainer.py
            rnn_hidden_states,
            prev_actions,
            masks,
            env_global_node,
            deterministic=False,
            return_features=False,
            mask_stop=False
    ):
    # observations['panoramic_rgb']: 64 x 252 x 3, observations['panoramic_depth']:  64 x 252 x 1, observations['target_goal']: 64 x 252 x 4
    # env_global_node: b x 1 x 512

    # features(xt): p(at|xt) = σ(FC(xt)) Size: num_processes x f_dim (512)
    
        features, rnn_hidden_states, preds, new_env_global_node, ffeatures, = self.net(
            observations, rnn_hidden_states, prev_actions, masks, env_global_node, return_features=return_features
        )

        distribution, x = self.action_distribution(features)
        value = self.critic(features) # uses a FC layer to map features to a scalar value of size num_processes x 1
        if deterministic:
            action = distribution.mode()
        else:
            action = distribution.sample()

        action_log_probs = distribution.log_probs(action)
        
        # The shape of the output should be B * N * (shapes)
        # NOTE: change distribution_entropy to x
        return value, action, action_log_probs, rnn_hidden_states, new_env_global_node, x, preds, ffeatures if return_features else None

    def get_value(self, observations, rnn_hidden_states, env_global_node, prev_actions, masks):
        """
        get the value of the current state which is represented by an observation
        """
        # features is the logits of action candidates
        features, *_ = self.net(
            observations, rnn_hidden_states, prev_actions, masks, env_global_node, disable_forgetting=True
        )
        value = self.critic(features)
        return value

    def evaluate_actions(
            self, observations, rnn_hidden_states, env_global_node, prev_actions, masks, action, return_features=False
    ):
        features, rnn_hidden_states, preds, env_global_node, ffeatures = self.net(
            observations, rnn_hidden_states, prev_actions, masks, env_global_node, return_features=return_features, disable_forgetting=True
        )
        distribution, x = self.action_distribution(features)
        value = self.critic(features)

        action_log_probs = distribution.log_probs(action)
        distribution_entropy = distribution.entropy().mean()

        return value, action_log_probs, distribution_entropy, preds[0], preds[1], ffeatures, rnn_hidden_states, env_global_node, x

    def get_memory_span(self):
        return self.net.get_memory_span()

    def get_forget_idxs(self):
        return self.net.perception_unit.forget_idxs


class CNNLSTMNet(nn.Module):
    def __init__(
            self,
            observation_space,
            action_space,
            goal_sensor_uuid,
            hidden_size,
            num_recurrent_layers,
            rnn_type,
            backbone,
            resnet_baseplanes,
            normalize_visual_inputs,
            cfg
    ):
        super().__init__()
        self.goal_sensor_uuid = goal_sensor_uuid
        self.prev_action_embedding = nn.Embedding(action_space.n + 1, 32)
        self._n_prev_action = 32

        self.num_category = 50
        self._n_input_goal = 0

        self._hidden_size = hidden_size

        rnn_input_size = self._n_input_goal + self._n_prev_action

        # Fvis is used to learn additional information related to navigation skills such as obstacle avoidance.
        # Fvis is based on ResNet-18 and trained along with the whole model.
        # It takes an image observation and produces a 512-dimension vector
        self.visual_encoder = ResNetEncoder(
            observation_space,
            baseplanes=resnet_baseplanes,
            ngroups=resnet_baseplanes // 2,
            make_backbone=getattr(resnet, backbone),
            normalize_visual_inputs=normalize_visual_inputs,
        )

        self.perception_unit = Perception(cfg)

        # visual_feature_dim and hidden_size are both default to 512
        f_dim = cfg.features.visual_feature_dim

        if not self.visual_encoder.is_blind:
            self.visual_fc = nn.Sequential(
                nn.Linear(
                    cfg.features.visual_feature_dim * 3, hidden_size * 2
                ),
                nn.ReLU(True),
                nn.Linear(
                    hidden_size * 2, hidden_size
                ),
                nn.ReLU(True),
            )

            self.pred_aux1 = nn.Sequential(nn.Linear(f_dim, f_dim),
                                    nn.ReLU(True),
                                    nn.Linear(f_dim, 1))
            self.pred_aux2 = nn.Sequential(nn.Linear(f_dim * 2, f_dim),
                                        nn.ReLU(True),
                                        nn.Linear(f_dim, 1))

        self.state_encoder = RNNStateEncoder(
            (0 if self.is_blind else self._hidden_size) + rnn_input_size,
            self._hidden_size,
            rnn_type=rnn_type,
            num_layers=num_recurrent_layers,
        )
        self.train()

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def is_blind(self):
        return self.visual_encoder.is_blind

    @property
    def num_recurrent_layers(self):
        return self.state_encoder.num_recurrent_layers

    def get_memory_span(self):
        return self.perception_unit.get_memory_span()
    
    def forward(self, observations, rnn_hidden_states, prev_actions, masks, env_global_node, mode='', return_features=False, disable_forgetting=False):
        prev_actions = self.prev_action_embedding(
            ((prev_actions.float() + 1) * masks).long().squeeze(-1)
        )

        input_list = [observations['panoramic_rgb'].permute(0, 3, 1, 2) / 255.0,
                      observations['panoramic_depth'].permute(0, 3, 1, 2)]
        curr_tensor = torch.cat(input_list, 1)
        observations['curr_embedding'] = self.visual_encoder(curr_tensor).view(curr_tensor.shape[0], -1) # B x 512
        goal_tensor = observations['target_goal'].permute(0, 3, 1, 2)
        observations['goal_embedding'] = self.visual_encoder(goal_tensor).view(goal_tensor.shape[0], -1)

        # curr_context: B x 512
        # goal_context: B x 512
        # new_env_global_node: B x 1 x 512
        #print(env_global_node[0:4,0,0:10])
        curr_context, goal_context, new_env_global_node, ffeatures = self.perception_unit(observations, env_global_node,
                                                                     return_features=return_features, disable_forgetting=disable_forgetting)
    
        contexts = torch.cat((curr_context, goal_context), -1)
        feats = self.visual_fc(torch.cat((contexts, observations['curr_embedding']), 1))
        pred1 = self.pred_aux1(curr_context)
        pred2 = self.pred_aux2(contexts)

        x = [feats, prev_actions]

        x = torch.cat(x, dim=1)
        x, rnn_hidden_states = self.state_encoder(x, rnn_hidden_states, masks)

        # x is used to generate the action prob distribution
        return x, rnn_hidden_states, (pred1, pred2), new_env_global_node, ffeatures # ffeatures contains att scores of GATv2 if required; otherwise ffeatures is None

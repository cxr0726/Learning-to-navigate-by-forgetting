import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from model.rnn_state_encoder import RNNStateEncoder
from custom_habitat_baselines.common.utils import CategoricalNet
from model.resnet import resnet
from model.resnet.resnet import ResNetEncoder
from .perception import Perception
from model.PCL.resnet_pcl import resnet18
class CriticHead(nn.Module):
    def __init__(self, input_size):
        super().__init__()
        self.fc = nn.Linear(input_size, 1)
        nn.init.orthogonal_(self.fc.weight)
        nn.init.constant_(self.fc.bias, 0)

    def forward(self, x):
        return self.fc(x)

class SMTPolicy(nn.Module):
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
        self.net = SMTNet(
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
        self.config = cfg
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
        print("================act==============")
        features, rnn_hidden_states, preds = self.net(
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
        return value, action, action_log_probs, rnn_hidden_states, None, x, preds, None

    def get_value(self, observations, rnn_hidden_states, env_global_node, prev_actions, masks):
        """
        get the value of the current state which is represented by an observation
        """
        # features is the logits of action candidates
        print("================get_value==============")

        features, *_ = self.net(
            observations, rnn_hidden_states, prev_actions, masks, env_global_node, disable_forgetting=True
        )
        value = self.critic(features)
        return value

    def evaluate_actions(
            self, observations, rnn_hidden_states, env_global_node, prev_actions, masks, action, return_features=False
    ):
        print("================evaluate_actions==============")
        features, rnn_hidden_states, preds = self.net(
            observations, rnn_hidden_states, prev_actions, masks, env_global_node, return_features=return_features, disable_forgetting=True
        )
        distribution, x = self.action_distribution(features)
        value = self.critic(features)

        action_log_probs = distribution.log_probs(action)
        distribution_entropy = distribution.entropy().mean()

        return value, action_log_probs, distribution_entropy, preds[0], preds[1], None, rnn_hidden_states, env_global_node, x

    def get_memory_span(self):
        return self.net.get_memory_span()

    def get_forget_idxs(self):
        return self.net.perception_unit.forget_idxs

class SMTNet(nn.Module):
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
        self.feature_dim = cfg.features.visual_feature_dim
        self.memory_dim = cfg.memory.embedding_size
        self.num_category = 50
        self._n_input_goal = 0
        self.device = 'cuda:' + str(cfg.TORCH_GPU_ID) if torch.cuda.device_count() > 0 else 'cpu'
        self._hidden_size = hidden_size

        rnn_input_size = self._n_input_goal + self._n_prev_action

        self.B = cfg.NUM_PROCESSES
        self.rgbd_encoder_backbone = resnet18(num_classes=self.feature_dim)
        dim_mlp = self.rgbd_encoder_backbone.fc.weight.shape[1] # 512
        self.rgbd_encoder_backbone.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.rgbd_encoder_backbone.fc)
        ckpt_pth = os.path.join('model/PCL', 'PCL_encoder.pth')
        ckpt = torch.load(ckpt_pth, map_location='cpu')
        self.rgbd_encoder_backbone.load_state_dict(ckpt)
        self.rgbd_encoder_backbone.eval()
        for p in self.rgbd_encoder_backbone.parameters():
            p.requires_grad = False
        
        self.rgbd_encoder = nn.Sequential(
            self.rgbd_encoder_backbone,
            nn.ReLU(True),
            nn.Linear(dim_mlp, self.memory_dim),
            nn.ReLU(True)
        )

        self.rgbd_encoder.to(self.device) # torchvision ResNet18
        # self.depth_encoder = resnet.resnet18(in_channels=1, base_planes=32, ngroups=32).to(self.device) # Custom ResNet18
        #self.seg_encoder = models.resnet18(num_classes=self.feature_dim).to(self.device)
        pose_feat_dim = 16
        act_emb_dim = 16

        self.pos_encoder = nn.Sequential(
            nn.Linear(5, pose_feat_dim),
            nn.ReLU(True)
        ).to(self.device)
        # self.action_emb = torch.nn.parameter.Parameter(
        #     torch.randn(len(cfg.TASK_CONFIG.TASK.POSSIBLE_ACTIONS), act_emb_dim),
        #     requires_grad=True
        #     )
        self.act_encoder = nn.Sequential(
            nn.Linear(self._n_prev_action, act_emb_dim),
            nn.ReLU(True)
        ).to(self.device)

        self.reduce = nn.Sequential(
            nn.Linear(self.memory_dim + pose_feat_dim + act_emb_dim, self.memory_dim),
            nn.ReLU(True)
        ).to(self.device)

        # Fvis is used to learn additional information related to navigation skills such as obstacle avoidance.
        # Fvis is based on ResNet-18 and trained along with the whole model.
        # It takes an image observation and produces a 512-dimension vector
        # self.visual_encoder = ResNetEncoder(
        #     observation_space,
        #     baseplanes=resnet_baseplanes,
        #     ngroups=resnet_baseplanes // 2,
        #     make_backbone=getattr(resnet, backbone),
        #     normalize_visual_inputs=normalize_visual_inputs,
        # )

        self.perception_unit = Perception(cfg)

        # visual_feature_dim and hidden_size are both default to 512
        f_dim = cfg.features.visual_feature_dim

        self.visual_fc = nn.Sequential(
            nn.Linear(
                self.memory_dim * 2 + self.feature_dim, hidden_size * 2
            ),
            nn.ReLU(True),
            nn.Linear(
                hidden_size * 2, hidden_size
            ),
            nn.ReLU(True),
        )

        self.pred_aux1 = nn.Sequential(nn.Linear(self.memory_dim, self.memory_dim),
                                nn.ReLU(True),
                                nn.Linear(self.memory_dim, 1))
        self.pred_aux2 = nn.Sequential(nn.Linear(self.memory_dim + self.feature_dim, self.memory_dim),
                                    nn.ReLU(True),
                                    nn.Linear(self.memory_dim, 1))

        self.state_encoder = RNNStateEncoder(
            self._hidden_size + rnn_input_size,
            self._hidden_size,
            rnn_type=rnn_type,
            num_layers=num_recurrent_layers,
        )
        self.train()
        self.calc_params()
    
    def calc_params(self):
        s = "- rgbd encoder: {}\n".format(sum(p.numel() for p in self.rgbd_encoder.parameters()))
        s += "- pose encoder: {}\n".format(sum(p.numel() for p in self.pos_encoder.parameters()))
        s += "- action encoder: {}\n".format(sum(p.numel() for p in self.act_encoder.parameters()))
        s += "- reduce encoder: {}\n".format(sum(p.numel() for p in self.reduce.parameters()))
        s += "- Perception: {}\n".format(sum(p.numel() for p in self.perception_unit.parameters()))
        s += "- visual_fc: {}\n".format(sum(p.numel() for p in self.visual_fc.parameters()))
        s += "- state_encoder: {}\n".format(sum(p.numel() for p in self.state_encoder.parameters()))
        s += "- aux tasks: {}\n".format(sum(p.numel() for p in self.pred_aux1.parameters()) + sum(p.numel() for p in self.pred_aux2.parameters()))

        print(s)
    
    @property
    def output_size(self):
        return self._hidden_size

    @property
    def is_blind(self):
        return False

    @property
    def num_recurrent_layers(self):
        return self.state_encoder.num_recurrent_layers

    def embed_obs_batch(self, obs_batch, b, step):
        # obs_batch contains:
        # ('panoramic_rgb_history', torch.Size([257, 4, 64, 252, 3])),
        # ('panoramic_depth_history', torch.Size([257, 4, 64, 252, 1])),
        # ('gps_history', torch.Size([257, 4, 2])),
        # ('compass_history', torch.Size([257, 4, 1])),
        # ('prev_action_history', torch.Size([257, 4, 1]))

        #print('\n***********\nglobal_memory', obs_batch['global_memory'].shape)
        global_memory_idxs = torch.nonzero(obs_batch['global_memory'][step] == True).squeeze(-1).to(self.device) # num_memory
        global_memory_idxs_print = global_memory_idxs.detach()
        num_memories = global_memory_idxs.shape[0]

        #print(obs_batch['panoramic_rgb_history'].shape, global_memory_idxs, b)
        # print(
        #     obs_batch['panoramic_rgb_history'].shape,
        #     obs_batch['panoramic_depth_history'].shape,
        #     obs_batch['gps_history'].shape,
        #     obs_batch['compass_history'].shape,
        #     obs_batch['prev_action_history'].shape,
        #     global_memory_idxs_print, b
        #     )
        rgb_tensor = obs_batch['panoramic_rgb_history'][global_memory_idxs, b].permute(0,3,1,2) / 255.0 # num_memory x 3 x 64 x 252
        depth_tensor = obs_batch['panoramic_depth_history'][global_memory_idxs, b].permute(0,3,1,2) # num_memory x 1 x 64 x 252
        # seg_tensor = obs_batch['panoramic_semantic'].permute(0,3,1,2)

        rgbd_feature = F.normalize(self.rgbd_encoder(torch.cat([rgb_tensor, depth_tensor], dim=1)).view(num_memories,-1),dim=1) # B x 512
        
        pos_tensor = obs_batch['gps_history'][global_memory_idxs, b] # num_memory x 2
        ori_tensor = obs_batch['compass_history'][global_memory_idxs, b] # num_memory x 1

        pos_vector = torch.cat([
            pos_tensor[:,0:1] / 5,
            pos_tensor[:,1:2] / 5,
            torch.cos(ori_tensor),
            torch.sin(ori_tensor),
            torch.exp(-global_memory_idxs.unsqueeze(-1).float())
            ], dim=-1) # num_memory x 5
        
        pos_feature = self.pos_encoder(pos_vector).view(num_memories,-1) 
        #print('global_memory_idxs', obs_batch['prev_action_history'].shape, global_memory_idxs.shape)
        prev_act_idxs = obs_batch['prev_action_history'][global_memory_idxs, b].squeeze(-1)
        #print(prev_act_idxs.shape)
        prev_action_feature = self.act_encoder(self.prev_action_embedding(prev_act_idxs + 1).to(self.device)) 

        #print(rgbd_feature.shape, pos_feature.shape, prev_action_feature.shape)
        obs_emb_batch = self.reduce(torch.cat([
            rgbd_feature,
            # nn.functional.normalize(self.depth_encoder().view(self.B,-1),dim=1),
            # nn.functional.normalize(self.seg_encoder(seg_tensor).view(self.B,-1),dim=1),
            pos_feature,
            prev_action_feature
        ], dim=-1))
        return obs_emb_batch

    def forward(self, observations, rnn_hidden_states, prev_actions, masks, env_global_node, mode='', return_features=False, disable_forgetting=False):
        # prev_actions: B x 1 (float)
        #print(self.prev_action_embedding.weight.shape, prev_actions)
        prev_actions = self.prev_action_embedding(
            ((prev_actions.float() + 1) * masks).long().squeeze(-1)
        )

        global_memory, curr_embeddings = [], []
        #print('prev_actions',prev_actions.shape)
        
        num_samples = prev_actions.shape[0] # 256 * 4
        num_step_per_batch = num_samples // self.B # 256

        for b in range(self.B):
            for step in range(num_step_per_batch):
                obs_emb_batch = self.embed_obs_batch(obs_batch=observations, b=b, step=b*num_step_per_batch + step)
                curr_embedding = obs_emb_batch[-1:]

                global_memory.append(obs_emb_batch)
                curr_embeddings.append(curr_embedding)

        global_memory = pad_sequence(global_memory, batch_first=True) # B x max_memory_num x 512
        curr_embedding = torch.stack(curr_embeddings) # B x 1 x 512

        # curr_context: B x 512
        # goal_context: B x 512
        # new_env_global_node: B x 1 x 512
        #print(env_global_node[0:4,0,0:10])
        observations['global_memory'] = global_memory # B x 512
        observations['curr_embedding'] = curr_embedding

        curr_context = self.perception_unit(observations, env_global_node) # B x 256
         # B x memory_size, True denotes the agent recorded an observation at that time step
        
        contexts = torch.cat((curr_context, observations['goal_embedding']), -1)

        feats = self.visual_fc(torch.cat((contexts, curr_embedding.squeeze(1)), 1))
        pred1 = self.pred_aux1(curr_context)
        pred2 = self.pred_aux2(contexts)

        #print(new_env_global_node[0:4,0,0:10])

        x = [feats, prev_actions]

        x = torch.cat(x, dim=1)
        x, rnn_hidden_states = self.state_encoder(x, rnn_hidden_states, masks)

        # x is used to generate the action prob distribution
        return x, rnn_hidden_states, (pred1, pred2) # ffeatures contains att scores of GATv2 if required; otherwise ffeatures is None

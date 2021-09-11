import torch
import torch.nn as nn
import torch.nn.functional as F
import resnet

class DMTL(nn.Module):
    def __init__(self, task_num, base_net='resnet50', hidden_dim=1024, class_num=31):
        super(DMTL, self).__init__()
        # base network
        self.base_network = resnet.__dict__[base_net](pretrained=True)
        # shared layer
        self.avgpool = self.base_network.avgpool
        self.hidden_layer_list = [nn.Linear(2048, hidden_dim),
                                  nn.BatchNorm1d(hidden_dim), nn.ReLU(), nn.Dropout(0.5)]
        self.hidden_layer = nn.Sequential(*self.hidden_layer_list)
        # task-specific layer
        self.classifier_parameter = nn.Parameter(torch.FloatTensor(task_num, hidden_dim, class_num))

        # initialization
        self.hidden_layer[0].weight.data.normal_(0, 0.005)
        self.hidden_layer[0].bias.data.fill_(0.1)
        self.classifier_parameter.data.normal_(0, 0.01)

    def forward(self, inputs, task_index):
        features = self.base_network(inputs)
        features = torch.flatten(self.avgpool(features), 1)
        hidden_features = self.hidden_layer(features)
        outputs = torch.mm(hidden_features, self.classifier_parameter[task_index])
        return outputs

    def predict(self, inputs, task_index):
        return self.forward(inputs, task_index)
        

class MTAN_ResNet(nn.Module):
    def __init__(self, task_num, num_classes):
        super(MTAN_ResNet, self).__init__()
        backbone = resnet.__dict__['resnet50'](pretrained=True)
        self.task_num = task_num
        # filter = [64, 128, 256, 512]   # for resent18
        filter = [256, 512, 1024, 2048]

        self.conv1, self.bn1, self.relu1, self.maxpool = backbone.conv1, backbone.bn1, backbone.relu, backbone.maxpool
        self.layer1 = backbone.layer1
        self.layer2 = backbone.layer2
        self.layer3 = backbone.layer3
        self.layer4 = backbone.layer4
        self.avgpool = backbone.avgpool

        self.linear = nn.ModuleList([nn.Linear(filter[-1], num_classes) for _ in range(self.task_num)])

        # attention modules
        self.encoder_att = nn.ModuleList([nn.ModuleList([self.att_layer([filter[0], filter[0], filter[0]])])])
        self.encoder_block_att = nn.ModuleList([self.conv_layer([filter[0], filter[1]])])

        for j in range(self.task_num):
            if j < self.task_num-1:
                self.encoder_att.append(nn.ModuleList([self.att_layer([filter[0], filter[0], filter[0]])]))

            for i in range(3):
                self.encoder_att[j].append(self.att_layer([2 * filter[i + 1], filter[i + 1], filter[i + 1]]))

        for i in range(3):
            if i < 2:
                self.encoder_block_att.append(self.conv_layer([filter[i + 1], filter[i + 2]]))
            else:
                self.encoder_block_att.append(self.conv_layer([filter[i + 1], filter[i + 1]]))

    def forward(self, x, k):
        g_encoder = [0] * 4

        atten_encoder = [0] * 4
        for i in range(4):
            atten_encoder[i] = [0] * 3

        # shared encoder
        x = self.maxpool(self.relu1(self.bn1(self.conv1(x))))
        g_encoder[0] = self.layer1(x)
        g_encoder[1] = self.layer2(g_encoder[0])
        g_encoder[2] = self.layer3(g_encoder[1])
        g_encoder[3] = self.layer4(g_encoder[2])

        # apply attention modules
        for j in range(4):
            if j == 0:
                atten_encoder[j][0] = self.encoder_att[k][j](g_encoder[0])
                atten_encoder[j][1] = (atten_encoder[j][0]) * g_encoder[0]
                atten_encoder[j][2] = self.encoder_block_att[j](atten_encoder[j][1])
                atten_encoder[j][2] = F.max_pool2d(atten_encoder[j][2], kernel_size=2, stride=2)
            else:
                atten_encoder[j][0] = self.encoder_att[k][j](torch.cat((g_encoder[j], atten_encoder[j - 1][2]), dim=1))
                atten_encoder[j][1] = (atten_encoder[j][0]) * g_encoder[j]
                atten_encoder[j][2] = self.encoder_block_att[j](atten_encoder[j][1])
                if j < 3:
                    atten_encoder[j][2] = F.max_pool2d(atten_encoder[j][2], kernel_size=2, stride=2)

        pred = self.avgpool(atten_encoder[-1][-1])
        pred = pred.view(pred.size(0), -1)

        out = self.linear[k](pred)
        return out
        
    def predict(self, x, k):
        return self.forward(x, k)
    
    def conv_layer(self, channel):
        conv_block = nn.Sequential(
            nn.Conv2d(in_channels=channel[0], out_channels=channel[1], kernel_size=3, padding=1),
            nn.BatchNorm2d(num_features=channel[1]),
            nn.ReLU(inplace=True),
        )
        return conv_block

    def att_layer(self, channel):
        att_block = nn.Sequential(
            nn.Conv2d(in_channels=channel[0], out_channels=channel[1], kernel_size=1, padding=0),
            nn.BatchNorm2d(channel[1]),
            nn.ReLU(inplace=True),
            nn.Conv2d(in_channels=channel[1], out_channels=channel[2], kernel_size=1, padding=0),
            nn.BatchNorm2d(channel[2]),
            nn.Sigmoid(),
        )
        return att_block
        

class AMTL(nn.Module):
    def __init__(self, task_num, base_net='resnet50', hidden_dim=1024, class_num=31, version='v1'):
        super(AMTL, self).__init__()
        # shared base network
        self.base_network_s = resnet.__dict__[base_net](pretrained=True)
        # task-specific base network
        self.base_network_t = nn.ModuleList([resnet.__dict__[base_net](pretrained=True) for _ in range(task_num)])
        self.avgpool = self.base_network_s.avgpool
        # shared hidden layer
        self.hidden_layer_list_s = [nn.Linear(2048, hidden_dim),nn.BatchNorm1d(hidden_dim), nn.ReLU(), nn.Dropout(0.5)]
        self.hidden_layer_s = nn.Sequential(*self.hidden_layer_list_s)
        # task-specific hidden layer
        self.hidden_layer_list_t = [nn.ModuleList([nn.Linear(2048, hidden_dim),nn.BatchNorm1d(hidden_dim), nn.ReLU(), nn.Dropout(0.5)]) for _ in range(task_num)]
        self.hidden_layer_t = nn.ModuleList([nn.Sequential(*self.hidden_layer_list_t[t]) for t in range(task_num)])
        # classifier layer
        self.classifier_parameter = nn.Parameter(torch.FloatTensor(task_num, hidden_dim, class_num))

        # initialization
        self.hidden_layer_s[0].weight.data.normal_(0, 0.005)
        self.hidden_layer_s[0].bias.data.fill_(0.1)
        self.classifier_parameter.data.normal_(0, 0.01)
        for t in range(task_num):
            self.hidden_layer_t[t][0].weight.data.normal_(0, 0.005)
            self.hidden_layer_t[t][0].bias.data.fill_(0.1)
        
        self.version = version
        
        # adaptative parameters
        if self.version == 'v1' or self.version =='v2':
            # AMTL-v1 and v2
            self.alpha = nn.Parameter(torch.FloatTensor(task_num, 2))
            self.alpha.data.fill_(0.5)   # init 0.5(shared) 0.5(specific)
            # self.alpha.data[:,0].fill_(0)  # shared
            # self.alpha.data[:,1].fill_(1)  # specific
        elif self.version == 'v3':
            # AMTL-v3, gumbel softmax
            self.alpha = nn.Parameter(torch.FloatTensor(task_num))
            self.alpha.data.fill_(0)
        else:
            print("No correct version parameter!")
            exit()

    def forward(self, inputs, task_index):
        features_s = self.base_network_s(inputs)
        features_s = torch.flatten(self.avgpool(features_s), 1)
        hidden_features_s = self.hidden_layer_s(features_s)
        
        features_t = self.base_network_t[task_index](inputs)
        features_t = torch.flatten(self.avgpool(features_t), 1)
        hidden_features_t = self.hidden_layer_t[task_index](features_t)
        
        if self.version == 'v1':
            temp_alpha = F.softmax(self.alpha[task_index], 0)     # AMTL-v1,  alpha_1 + alpha_2 = 1
        elif self.version == 'v2':
            temp_alpha = torch.exp(self.alpha[task_index]) / (1 + torch.exp(self.alpha[task_index])) # AMTL-v2,  0 <= alpha <=1
        elif self.version == 'v3':
            # below for AMTL-v3, gumbel softmax
            temp = torch.sigmoid(self.alpha[task_index])
            temp_alpha = torch.stack([1-temp, temp])
            temp_alpha = F.gumbel_softmax(torch.log(temp_alpha), tau=0.1, hard=True)
        else:
            print("No correct version parameter!")
            exit()

        hidden_features = temp_alpha[0] * hidden_features_s + temp_alpha[1] * hidden_features_t
        
        outputs = torch.mm(hidden_features, self.classifier_parameter[task_index])
        return outputs
    
    def predict(self, inputs, task_index):
        features_s = self.base_network_s(inputs)
        features_s = torch.flatten(self.avgpool(features_s), 1)
        hidden_features_s = self.hidden_layer_s(features_s)
        
        features_t = self.base_network_t[task_index](inputs)
        features_t = torch.flatten(self.avgpool(features_t), 1)
        hidden_features_t = self.hidden_layer_t[task_index](features_t)
        
        if self.version == 'v1':
            temp_alpha = F.softmax(self.alpha[task_index], 0)     # AMTL-v1,  alpha_1 + alpha_2 = 1
        elif self.version == 'v2':
            temp_alpha = torch.exp(self.alpha[task_index]) / (1 + torch.exp(self.alpha[task_index])) # AMTL-v2,  0 <= alpha <=1
        elif self.version == 'v3':
            # below for AMTL-v3, gumbel softmax
            temp = torch.sigmoid(self.alpha[task_index])
            if temp >= 0.5:
                temp_alpha = [0, 1]
            else:
                temp_alpha = [1, 0]
        else:
            print("No correct version parameter!")
            exit()

        hidden_features = temp_alpha[0] * hidden_features_s + temp_alpha[1] * hidden_features_t
        
        outputs = torch.mm(hidden_features, self.classifier_parameter[task_index])
        return outputs
        
    def get_adaptative_parameter(self):
        return self.alpha
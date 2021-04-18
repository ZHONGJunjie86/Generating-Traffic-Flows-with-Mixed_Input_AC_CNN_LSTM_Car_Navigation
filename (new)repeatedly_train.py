from utils import cross_loss_curve, GAMA_connect,reset,send_to_GAMA
from CV_input import generate_img,generate_img_train
import os
from itertools import count
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.distributions import MultivariateNormal
import numpy as np
import pandas as pd
import warnings


warnings.filterwarnings("ignore")

device = torch.device("cuda" if torch.cuda.is_available() else"cpu")  

save_curve_pic = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/result/Actor_Critic_3loss_curve.png'
save_critic_loss = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/training_data/AC_critic_3loss.csv'
save_reward = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/training_data/AC_3reward.csv'
save_speed = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/training_data/AC_average_speed.csv'
save_NPC_speed =  os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/training_data/NPC_speed.csv'
state_size = 3
action_size = 1 
torch.set_default_tensor_type(torch.DoubleTensor)

class Memory:
    def __init__(self):
        self.actions = []
        self.states = []
        self.states_next = []
        self.states_img = []
        self.states_img_next = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []
    
    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.states_next[:]
        del self.states_img[:]
        del self.states_img_next[:]
        del self.logprobs[:]
        del self.rewards[:]
        del self.is_terminals[:]

class Actor(nn.Module):
    def __init__(self, state_size, action_size):
        super(Actor, self).__init__()
        self.conv1 = nn.Conv2d(3,8, kernel_size=8, stride=4, padding=0) # 500*500*3 -> 124*124*8
        self.maxp1 = nn.MaxPool2d(4, stride = 2, padding=0) # 124*124*8 -> 61*61*8
        self.conv2 = nn.Conv2d(8, 16, kernel_size=4, stride=1, padding=0) # 61*61*8 -> 58*58*16 
        self.maxp2 = nn.MaxPool2d(2, stride=2, padding=0) # 58*58*16  -> 29*29*16 = 13456
        self.linear_CNN_1 = nn.Linear(13456, 256)
        self.linear_CNN_2 = nn.Linear(768,256)
        
        #
        self.state_size = state_size
        self.action_size = action_size
        self.linear1 = nn.Linear(self.state_size, 128)
        self.linear2 = nn.Linear(128, 85)
        
        self.linear3 = nn.Linear(511,128)
        self.linear4 = nn.Linear(128,32)
        self.mu = nn.Linear(32,self.action_size)  #256 linear2
        self.sigma = nn.Linear(32,self.action_size)
        self.hidden_cell = (torch.zeros(1,1,64).to(device),
                            torch.zeros(1,1,64).to(device))

    def forward(self, state,tensor_cv):
        # CV
        x = F.relu(self.maxp1(self.conv1(tensor_cv)))
        x = F.relu(self.maxp2(self.conv2(x)))
        x = x.view(x.size(0), -1) #展開
        x = F.relu(self.linear_CNN_1(x)).reshape(1,768)
        x = F.relu(self.linear_CNN_2(x)).reshape(1,256)
        # num
        output_1 = F.relu(self.linear1(state))
        output_2 = F.relu(self.linear2(output_1)).reshape(1,255)
        # merge
        output_2 = torch.cat((x,output_2),1) 
        output_3 = F.relu(self.linear3(output_2) )
        #
        output_4 =F.relu(self.linear4(output_3)) #F.relu(self.linear4(output_3.view(-1,c))) #
        mu = torch.tanh(self.mu(output_4))   #有正有负 sigmoid 0-1
        sigma = F.relu(self.sigma(output_4)) + 0.001 
        mu = torch.diag_embed(mu).to(device)
        sigma = torch.diag_embed(sigma).to(device)  # change to 2D
        dist = MultivariateNormal(mu,sigma)  #N(μ，σ^2)
        entropy = dist.entropy().mean()
        action = dist.sample()
        action_logprob = dist.log_prob(action)     
        action = torch.clamp(action.detach(), -0.8, 0.6)
        return action,action_logprob

class Critic(nn.Module):
    def __init__(self, state_size, action_size):
        super(Critic, self).__init__()
        self.conv1 = nn.Conv2d(3,8, kernel_size=8, stride=4, padding=0) # 500*500*3 -> 124*124*8
        self.maxp1 = nn.MaxPool2d(4, stride = 2, padding=0) # 124*124*8 -> 61*61*8
        self.conv2 = nn.Conv2d(8, 16, kernel_size=4, stride=1, padding=0) # 61*61*8 -> 58*58*16 
        self.maxp2 = nn.MaxPool2d(2, stride=2, padding=0) # 58*58*16  -> 29*29*16 = 13456
        self.linear_CNN = nn.Linear(13456, 256)
        self.lstm_CNN = nn.LSTM(256,85, 1,batch_first=True) #768,255
        #
        self.state_size = state_size
        self.action_size = action_size
        self.linear1 = nn.Linear(self.state_size, 128)
        self.linear2 = nn.Linear(128, 128)
        self.lstm3 = nn.LSTM(128,85,1, batch_first=True)
        #
        self.LSTM_layer_3 = nn.LSTM(510,128,1, batch_first=True)  
        self.linear4 = nn.Linear(128,32) #
        self.linear5 = nn.Linear(32, action_size)

    def forward(self, state, tensor_cv,h_state_cv_c=(torch.zeros(1,3,85).to(device),
                            torch.zeros(1,3,85).to(device)),h_state_n_c=(torch.zeros(1,3,85).to(device),
                            torch.zeros(1,3,85).to(device)),h_state_3_c=(torch.zeros(1,1,128).to(device),
                            torch.zeros(1,1,128).to(device))):
        # CV
        x = F.relu(self.maxp1(self.conv1(tensor_cv)))
        x = F.relu(self.maxp2(self.conv2(x))).reshape(3,1,13456)
        #x = x.view(x.size(0), -1) #展開
        x = F.relu(self.linear_CNN(x))#.reshape(1,768)
        x,h_state_cv_c = self.lstm_CNN(x ,h_state_cv_c)  # #.unsqueeze(0)
        x = F.relu( x).reshape(1,255)  #torch.tanh
        # num
        output_1 = F.relu(self.linear1(state))
        output_2 = F.relu(self.linear2(output_1))
        output_2,h_state_n_c = self.lstm3(output_2,h_state_n_c)   #
        output_2 = F.relu(output_2)  #
        output_2 = output_2.squeeze().reshape(1,255)
        # LSTM
        output_2 = torch.cat((x,output_2),1) 
        output_2  = output_2.unsqueeze(0)
        output_3 , h_state_3_c= self.LSTM_layer_3(output_2,h_state_3_c )   #    #,self.hidden_cell
        a,b,c = output_3.shape
        #
        output_4 = F.relu(self.linear4(output_3.view(-1,c))) 
        value  = torch.tanh(self.linear5(output_4))
        return value ,(h_state_cv_c[0].data,h_state_cv_c[1].data),(h_state_n_c[0].data,h_state_n_c[1].data),(h_state_3_c[0].data,h_state_3_c[1].data)

def main():
    ################ load ###################
    #train_agent
    actor_train_path = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/weight/AC_TD3_actor.pkl'
    critic_train_path = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/weight/AC_TD3_critic.pkl'
    if os.path.exists(actor_train_path):
        actor_train =  Actor(state_size, action_size).to(device)
        actor_train.load_state_dict(torch.load(actor_train_path))
        print('Actor_Train Model loaded')
    else:
        actor_train = Actor(state_size, action_size).to(device)
    if os.path.exists(critic_train_path):
        critic_train = Critic(state_size, action_size).to(device)
        critic_train.load_state_dict(torch.load(critic_train_path))
        print('Critic_Train Model loaded')
    else:
        critic_train = Critic(state_size, action_size).to(device)
    critic_next_train = Critic(state_size, action_size).to(device)
    critic_next_train.load_state_dict(critic_train.state_dict())
    #agents
    actor_path = os.path.abspath(os.curdir)+'/Generate_Traffic_Flow_MAS_RL/weight/AC_TD_MAS_actor.pkl'
    if os.path.exists(actor_path):
        actor =  Actor(state_size, action_size).to(device)
        actor.load_state_dict(torch.load(actor_path))
        print('Actor Model loaded')
    
    print("Waiting for GAMA...")

    ################### initialization ########################
    reset()

    episode = 0

    training_stage = 65

    lr = 0.0001

    sample_lr = [
        0.0001, 0.00009, 0.00008, 0.00007, 0.00006, 0.00005, 0.00004, 0.00003,
        0.00002, 0.00001, 0.000009, 0.000008, 0.000007, 0.000006, 0.000005,
        0.000004, 0.000003, 0.000002, 0.000001
    ]
    if episode >training_stage  : #50 100
        try:
            lr = sample_lr[int(episode //training_stage)]*0.01
        except(IndexError):
            lr = 0.000001*0.9#* (0.9 ** ((episode-1000) // 60)) 

    optimizerA = optim.Adam(actor_train.parameters(), lr, betas=(0.95, 0.999))
    optimizerC = optim.Adam(critic_train.parameters(), lr, betas=(0.95, 0.999))

    values = []
    rewards = []
    masks = []
    total_loss = []
    total_rewards = []
    loss = []
    average_speed = []

    value = 0
    gama = 0.9
    over = 0
    log_prob = 0
    memory  = Memory ()

    A_T,state,reward,done,time_pass,over,average_speed_NPC = GAMA_connect( )
    print("Connected")
    ##################  start  #########################
    while over!= 1:
        #training_agent
        if A_T == 0:
            #普通の場合
            average_speed.append(state[0])
            if(done == 0 and time_pass != 0):  
                #前回の報酬
                reward = torch.tensor([reward], dtype=torch.float, device=device)
                rewards.append(reward)   
                state = torch.DoubleTensor(state).reshape(1,state_size).to(device)
                state_img = generate_img_train() 
                tensor_cv = torch.from_numpy(np.transpose(state_img, (2, 0, 1))).double().to(device)/255
                if  len(memory.states_next) ==0:
                    #for _ in range(3):
                    memory.states_next = memory.states
                    memory.states_next[2] = state
                    memory.states_img_next = memory.states_img
                    memory.states_img_next [2]= tensor_cv
                else:
                    del memory.states_next[:1]
                    del memory.states_img_next[:1]
                    memory.states_next.append(state)
                    memory.states_img_next.append(tensor_cv)
                
                state_next = torch.stack(memory.states_next).to(device).detach()
                tensor_cv_next = torch.stack(memory.states_img_next).to(device).detach()  
                value_next,_,_,_ = critic_next_train(state_next,tensor_cv_next,h_state_cv_c,h_state_n_c,h_state_3_c )   #_next
                with torch.autograd.set_detect_anomaly(True):
                    # TD:r(s) +  gama*v(s+1) - v(s)
                    advantage = reward.detach() + gama*value_next.detach() - value 
                    actor_loss = -(log_prob * advantage.detach())     
                    critic_loss = (reward.detach() + gama*value_next.detach() - value).pow(2) 
                    optimizerA.zero_grad()
                    optimizerC.zero_grad()
                    critic_loss.backward()  
                    actor_loss.backward()
                    loss.append(critic_loss)
                    optimizerA.step()
                    optimizerC.step()
                    critic_next_train.load_state_dict(critic_train.state_dict())

                del  memory.states[:1]
                del  memory.states_img[:1]
                memory.states.append(state)
                memory.states_img.append(tensor_cv)
                state = torch.stack(memory.states).to(device).detach()  
                tensor_cv = torch.stack(memory.states_img).to(device).detach()
                value,h_state_cv_c,h_state_n_c,h_state_3_c =  critic_train(state,tensor_cv,h_state_cv_c,h_state_n_c,h_state_3_c)  
                action,log_prob = actor_train(state,tensor_cv) 
                log_prob = log_prob.unsqueeze(0)           

                send_to_GAMA([[1,float(action.cpu().numpy()*10)]]) #行
                masks.append(torch.tensor([1-done], dtype=torch.float, device=device))  
                values.append(value)

            # 終わり 
            elif done == 1:
                average_speed.append(state[0])
                send_to_GAMA([[1,0]])
                #先传后计算
                print(state)
                rewards.append(reward)  #contains the last
                reward = torch.tensor([reward], dtype=torch.float, device=device)
                rewards.append(reward)  #contains the last
                total_reward = sum(rewards).cpu().detach().numpy()
                total_rewards.append(total_reward)

                with torch.autograd.set_detect_anomaly(True):
                    advantage = reward.detach() - value            #+ last_value   最后一回的V(s+1) = 0
                    actor_loss = -( log_prob * advantage.detach())    
                    critic_loss = (reward.detach()  - value).pow(2)  #+ last_value

                    optimizerA.zero_grad()
                    optimizerC.zero_grad()

                    critic_loss.backward() 
                    actor_loss.backward()
                    loss.append(critic_loss)
                    
                    optimizerA.step()
                    optimizerC.step()

                    critic_next_train.load_state_dict(critic_train.state_dict())

                print("----------------------------------Net_Trained---------------------------------------")
                print('--------------------------Iteration:',episode,'over--------------------------------')
                episode += 1
                values = []
                rewards = []
                loss_sum = sum(loss).cpu().detach().numpy()
                total_loss.append(loss_sum)
                cross_loss_curve(loss_sum.squeeze(0),total_reward,save_curve_pic,save_critic_loss,save_reward, np.mean(average_speed),save_speed, average_speed_NPC, save_NPC_speed) 
                #total_loss,total_rewards#np.mean(average_speed)/10
                loss = []
                average_speed  = []
                memory.clear_memory()

                torch.save(actor_train.state_dict(),actor_train_path)
                torch.save(critic_train.state_dict(),critic_train_path)

                if episode >training_stage  : #50 100
                    try:
                        lr = sample_lr[int(episode //training_stage)]*0.01
                    except(IndexError):
                        lr = 0.000001*0.9#* (0.9 ** ((episode-1000) // 60)) 

                optimizerA = optim.Adam(actor_train.parameters(), lr, betas=(0.95, 0.999))
                optimizerC = optim.Adam(critic_train.parameters(), lr, betas=(0.95, 0.999))

            #最初の時
            if time_pass == 0:  
                print('Iteration:',episode,"lr:",lr)
                state = np.reshape(state,(1,len(state))) 
                state_img = generate_img_train() 
                tensor_cv = torch.from_numpy(np.transpose(state_img, (2, 0, 1))).double().to(device)/255
                state = torch.DoubleTensor(state).reshape(1,state_size).to(device)
            
                for _ in range(3):
                    memory.states.append(state)
                    memory.states_img.append(tensor_cv)

                state = torch.stack(memory.states).to(device).detach() ###
                tensor_cv = torch.stack(memory.states_img).to(device).detach()
                value,h_state_cv_c,h_state_n_c,h_state_3_c =  critic_train(state,tensor_cv)  #dist,  # now is a tensoraction = dist.sample() 
                action,log_prob = actor_train(state,tensor_cv)  
                print("acceleration: ",action.cpu().numpy())
                send_to_GAMA([[1,float(action.cpu().numpy()*10)]])
        
        #agents
        if A_T == 1:
            state = [torch.DoubleTensor(elem).reshape(1,state_size).to(device) for elem in state]  
            state = torch.stack(state).to(device).detach()
            tensor_cv_MAS = generate_img() 
            tensor_cv_MAS = [torch.from_numpy(np.transpose(elem, (2, 0, 1))).double().to(device)/255 for elem in tensor_cv_MAS]  
            tensor_cv_MAS = torch.stack(tensor_cv_MAS).to(device).detach()

            action,_ = actor(state,tensor_cv_MAS)
            
            send_to_GAMA([[1,float(action.cpu().numpy()*10)]])
        
        A_T,state,reward,done,time_pass,over,average_speed_NPC = GAMA_connect()

    return None 

if __name__ == '__main__':
    main()
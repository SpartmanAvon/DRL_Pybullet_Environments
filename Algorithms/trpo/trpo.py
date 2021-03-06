import gym
import pybullet_envs
import torch
import numpy as np
import time
import argparse
import os
import imageio

from core import MLPActorCritic
from gae_buffer import GAEBuffer
# from Algorithms.trpo.core import MLPActorCritic
# from Algorithms.trpo.gae_buffer import GAEBuffer
# from Logger.logger import Logger
from copy import deepcopy
from torch import optim
from tqdm import tqdm

class TRPO:
    
    def __init__(self, env_fn, actor_critic=MLPActorCritic, ac_kwargs=dict(), seed=0, 
         steps_per_epoch=400, gamma=0.99, delta=0.01, vf_lr=1e-3,
         train_v_iters=80, damping_coeff=0.1, cg_iters=10, backtrack_iters=10, 
         backtrack_coeff=0.8, lam=0.97, max_ep_len=1000, logger_kwargs=dict(), 
         save_freq=10, algo='trpo'):
        """
        Trust Region Policy Optimization 
        (with support for Natural Policy Gradient)
        Args:
            env_fn : A function which creates a copy of the environment.
                The environment must satisfy the OpenAI Gym API.
            actor_critic: Class for the actor-critic pytorch module
            ac_kwargs (dict): Any kwargs appropriate for the actor_critic 
                function you provided to TRPO.
            seed (int): Seed for random number generators.
            steps_per_epoch (int): Number of steps of interaction (state-action pairs) 
                for the agent and the environment in each epoch.
            gamma (float): Discount factor. (Always between 0 and 1.)
            delta (float): KL-divergence limit for TRPO / NPG update. 
                (Should be small for stability. Values like 0.01, 0.05.)
            vf_lr (float): Learning rate for value function optimizer.
            train_v_iters (int): Number of gradient descent steps to take on 
                value function per epoch.
            damping_coeff (float): Artifact for numerical stability, should be 
                smallish. Adjusts Hessian-vector product calculation:
                
                .. math:: Hv \\rightarrow (\\alpha I + H)v
                where :math:`\\alpha` is the damping coefficient. 
                Probably don't play with this hyperparameter.
            cg_iters (int): Number of iterations of conjugate gradient to perform. 
                Increasing this will lead to a more accurate approximation
                to :math:`H^{-1} g`, and possibly slightly-improved performance,
                but at the cost of slowing things down. 
                Also probably don't play with this hyperparameter.
            backtrack_iters (int): Maximum number of steps allowed in the 
                backtracking line search. Since the line search usually doesn't 
                backtrack, and usually only steps back once when it does, this
                hyperparameter doesn't often matter.
            backtrack_coeff (float): How far back to step during backtracking line
                search. (Always between 0 and 1, usually above 0.5.)
            lam (float): Lambda for GAE-Lambda. (Always between 0 and 1,
                close to 1.)
            max_ep_len (int): Maximum length of trajectory / episode / rollout.
            logger_kwargs (dict): Keyword args for Logger. 
                            (1) output_dir = None
                            (2) output_fname = 'progress.pickle'
            save_freq (int): How often (in terms of gap between epochs) to save
                the current policy and value function.
            algo: Either 'trpo' or 'npg': this code supports both, since they are 
                almost the same.
        """
        # logger stuff
        # self.logger = Logger(**logger_kwargs)

        torch.manual_seed(seed)
        np.random.seed(seed)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device="cpu"
        self.env = env_fn()
        self.vf_lr = vf_lr
        self.steps_per_epoch = steps_per_epoch
        # self.epochs = epochs
        self.max_ep_len = self.env.spec.max_episode_steps if self.env.spec.max_episode_steps is not None else max_ep_len
        self.train_v_iters = train_v_iters

        # Main network
        self.ac = actor_critic(self.env.observation_space, self.env.action_space, device=self.device, **ac_kwargs)

        # Create Optimizers
        self.v_optimizer = optim.Adam(self.ac.v.parameters(), lr=self.vf_lr)

        # GAE buffer
        self.gamma = gamma
        self.lam = lam
        self.obs_dim = self.env.observation_space.shape
        self.act_dim = self.env.action_space.shape
        self.buffer = GAEBuffer(self.obs_dim, self.act_dim, self.steps_per_epoch, self.device, self.gamma, self.lam)

        self.cg_iters = cg_iters
        self.damping_coeff = damping_coeff
        self.delta = delta
        self.backtrack_coeff = backtrack_coeff
        self.algo = algo
        self.backtrack_iters = backtrack_iters

    def flat_grad(self, grads, hessian=False):
        grad_flatten = []
        if hessian == False:
            for grad in grads:
                grad_flatten.append(grad.view(-1))
            grad_flatten = torch.cat(grad_flatten)
            return grad_flatten
        elif hessian == True:
            for grad in grads:
                grad_flatten.append(grad.contiguous().view(-1))
            grad_flatten = torch.cat(grad_flatten).data
            return grad_flatten

    def cg(self, obs, b, act, EPS=1e-8, residual_tol=1e-10):
        # Conjugate gradient algorithm
        # (https://en.wikipedia.org/wiki/Conjugate_gradient_method)
        x = torch.zeros(b.size()).to(self.device)
        r = b.clone()
        p = r.clone()
        rdotr = torch.dot(r, r).to(self.device)

        for _ in range(self.cg_iters):
            Ap = self.hessian_vector_product(obs, p)
            alpha = rdotr / (torch.dot(p, Ap).to(self.device) + EPS)
            
            x += alpha * p
            r -= alpha * Ap
            
            new_rdotr = torch.dot(r, r)
            p = r + (new_rdotr / rdotr) * p
            rdotr = new_rdotr

            if rdotr < residual_tol:
                break

        return x


    def hessian_vector_product(self, obs, p):
        p = p.detach()
        kl = self.ac.pi.calculate_kl(old_policy=self.ac.pi, new_policy=self.ac.pi, obs=obs)
        kl_grad = torch.autograd.grad(kl, self.ac.pi.parameters(), create_graph=True)
        kl_grad = self.flat_grad(kl_grad)

        kl_grad_p = (kl_grad * p).sum() 
        kl_hessian = torch.autograd.grad(kl_grad_p, self.ac.pi.parameters())
        kl_hessian = self.flat_grad(kl_hessian, hessian=True)
        return kl_hessian + p * self.damping_coeff

    def flat_params(self, model):
        params = []
        for param in model.parameters():
            params.append(param.data.view(-1))
        params_flatten = torch.cat(params)
        return params_flatten

    def update_model(self, model, new_params):
        index = 0
        for params in model.parameters():
            params_length = len(params.view(-1))
            new_param = new_params[index: index + params_length]
            new_param = new_param.view(params.size())
            params.data.copy_(new_param)
            index += params_length

    def get_action(self, obs):
        '''
        Input the current observation into the actor network to calculate action to take.
        Args:
            obs (numpy ndarray): Current state of the environment
        Return:
            Action (numpy ndarray): Scaled action that is clipped to environment's action limits
        '''
        obs = torch.as_tensor(obs, dtype=torch.float32).to(self.device)
        action = self.ac.act(obs)
        return action.detach().cpu().numpy()

    def update(self):
        data = self.buffer.get()
        obs = data['obs']
        act = data['act']
        ret = data['ret']
        adv = data['adv']
        logp_old = data['logp']

        # Prediction logπ_old(s), logπ(s)
        _, logp = self.ac.pi(obs, act)
        
        # Policy loss
        ratio_old = torch.exp(logp - logp_old)
        surrogate_adv_old = (ratio_old*adv).mean()
        
        # policy gradient calculation as per algorithm, flatten to do matrix calculations later
        gradient = torch.autograd.grad(surrogate_adv_old, self.ac.pi.parameters()) # calculate gradient of policy loss w.r.t to policy parameters
        gradient = self.flat_grad(gradient)

        # Core calculations for NPG/TRPO
        search_dir = self.cg(obs, gradient.data, act)    # H^-1 g
        gHg = (self.hessian_vector_product(obs, search_dir, act) * search_dir).sum(0)
        step_size = torch.sqrt(2 * self.delta / gHg)
        old_params = self.flat_params(self.ac.pi)
        # update the old model, calculate KL divergence then decide whether to update new model
        self.update_model(self.ac.pi_old, old_params)        

        if self.algo == 'npg':
            params = old_params + step_size * search_dir
            self.update_model(self.ac.pi, params)

            kl = self.ac.pi.calculate_kl(new_policy=self.ac.pi, old_policy=self.ac.pi_old, obs=obs)
        elif self.algo == 'trpo':
            # expected_improve = (gradient * step_size * search_dir).sum(0, keepdim=True)

            for i in range(self.backtrack_iters):
                # Backtracking line search
                # (https://web.stanford.edu/~boyd/cvxbook/bv_cvxbook.pdf) 464p.
                params = old_params + (self.backtrack_coeff**(i+1)) * step_size * search_dir
                # params = old_params + self.backtrack_coeff * step_size * search_dir
                self.update_model(self.ac.pi, params)


                # Prediction logπ_old(s), logπ(s)
                _, logp = self.ac.pi(obs, act)
                
                # Policy loss
                ratio = torch.exp(logp - logp_old)
                surrogate_adv = (ratio*adv).mean()

                improve = surrogate_adv - surrogate_adv_old
                # expected_improve *= self.backtrack_coeff
                # improve_condition = loss_improve / expected_improve

                kl = self.ac.pi.calculate_kl(new_policy=self.ac.pi, old_policy=self.ac.pi_old, obs=obs)
                
                if kl <= self.delta and improve>0:
                    print('Accepting new params at step %d of line search.'%i)
                    # self.backtrack_iters.append(i)
                    # log backtrack_iters=i
                    break

                if i == self.backtrack_iters-1:
                    print('Line search failed! Keeping old params.')
                    # self.backtrack_iters.append(i)
                    # log backtrack_iters=i

                    params = self.flat_params(self.ac.pi_old)
                    self.update_model(self.ac.pi, params)

                # self.backtrack_coeff *= 0.5


        # Update Critic
        for _ in range(self.train_v_iters):
            self.v_optimizer.zero_grad()
            v = self.ac.v(obs)
            v_loss = ((v-ret)**2).mean()
            v_loss.backward()
            self.v_optimizer.step()


    def learn(self, timesteps):
        ep_rets = []
        epochs = int((timesteps/self.steps_per_epoch) + 0.5)
        print("Rounded off to {} epochs with {} steps per epoch, total {} timesteps".format(epochs, self.steps_per_epoch, epochs*self.steps_per_epoch))
        start_time = time.time()
        obs, ep_ret, ep_len = self.env.reset(), 0, 0

        for epoch in tqdm(range(epochs)):
            for t in range(self.steps_per_epoch):
                # step the environment
                a, v, logp = self.ac.step(torch.as_tensor(obs, dtype=torch.float32).to(self.device))
                next_obs, reward, done, _ = self.env.step(a)
                ep_ret += reward
                ep_len += 1
                
                # Add experience to buffer
                self.buffer.store(obs, a, reward, v, logp)

                obs = next_obs
                timeout = ep_len == self.max_ep_len
                terminal = done or timeout
                epoch_ended = t==self.steps_per_epoch-1

                # End of trajectory/episode handling
                if terminal or epoch_ended:
                    if timeout or epoch_ended:
                        _, v, _ = self.ac.step(torch.as_tensor(obs, dtype=torch.float32).to(self.device))
                    else:
                        v = 0
                    
                    # print(f"Episode return: {ep_ret}")
                    ep_rets.append(ep_ret)
                    self.buffer.finish_path(v)
                    # if terminal:
                    #     # only save EpRet / EpLen if trajectory finished
                    #     logger.store(EpRet=ep_ret, EpLen=ep_len)
                    state, ep_ret, ep_len = self.env.reset(), 0, 0

            # self.buffer.get()
            # update value function and TRPO policy update
            print("average return: " + str(np.array(ep_rets[-10:]).mean()))
            self.update()


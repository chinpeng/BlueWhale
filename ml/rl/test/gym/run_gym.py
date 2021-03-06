from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import collections
import json
import sys

from caffe2.proto import caffe2_pb2
from caffe2.python import core

from ml.rl.test.gym.open_ai_gym_environment import ModelType, OpenAIGymEnvironment
from ml.rl.training.ddpg_trainer import DDPGTrainer
from ml.rl.training.continuous_action_dqn_trainer import ContinuousActionDQNTrainer
from ml.rl.training.discrete_action_trainer import DiscreteActionTrainer
from ml.rl.training.conv.discrete_action_conv_trainer import DiscreteActionConvTrainer
from ml.rl.thrift.core.ttypes import RLParameters, TrainingParameters,\
    DiscreteActionModelParameters, DiscreteActionConvModelParameters,\
    CNNModelParameters, ContinuousActionModelParameters, KnnParameters,\
    DDPGRLParameters, DDPGNetworkParameters, DDPGTrainingParameters,\
    DDPGModelParameters

USE_CPU = -1

EnvDetails = collections.namedtuple(
    'EnvDetails',
    ['state_dim', 'action_dim', 'action_range'])


def run(
    env,
    model_type,
    trainer,
    test_run_name,
    score_bar,
    num_episodes=301,
    max_steps=None,
    train_every=10,
    train_after=10,
    test_every=100,
    test_after=10,
    num_train_batches=100,
    avg_over_num_episodes=100,
    render=False,
    render_every=10,
):
    avg_reward_history = []

    predictor = trainer.predictor()

    for i in range(num_episodes):
        env.run_episode(model_type, predictor, max_steps, False,
            render and i % render_every == 0)
        if i % train_every == 0 and i > train_after:
            for _ in range(num_train_batches):
                if model_type == ModelType.CONTINUOUS_ACTION.value:
                    training_samples = env.sample_memories(trainer.minibatch_size)
                    trainer.train(predictor, training_samples)
                else:
                    env.sample_and_load_training_data_c2(
                        trainer.minibatch_size, model_type, trainer.maxq_learning)
                    trainer.train(reward_timelines=None, evaluator=None)
        if i == num_episodes - 1 or (i % test_every == 0 and i > test_after):
            reward_sum = 0.0
            for test_i in range(avg_over_num_episodes):
                reward_sum += env.run_episode(model_type, predictor, max_steps, True,
                    render and test_i % render_every == 0)
            avg_rewards = round(reward_sum / avg_over_num_episodes, 2)
            print(
                "Achieved an average reward score of {} over {} iterations"
                .format(avg_rewards, avg_over_num_episodes)
            )
            avg_reward_history.append(avg_rewards)
            if score_bar is not None and avg_rewards > score_bar:
                break

    print(
        'Averaged reward history for {}:'.format(test_run_name),
        avg_reward_history
    )
    return avg_reward_history


def main(args):
    parser = argparse.ArgumentParser(
        description="Train a RL net to play in an OpenAI Gym environment."
    )
    parser.add_argument(
        "-p",
        "--parameters",
        help="Path to JSON parameters file.",
    )
    parser.add_argument(
        "-s",
        "--score-bar",
        help="Bar for averaged tests scores.",
        type=float,
        default=None,
    )
    parser.add_argument(
        "-g",
        "--gpu_id",
        help="If set, will use GPU with specified ID. Otherwise will use CPU.",
        default=USE_CPU,
    )
    args = parser.parse_args(args)
    with open(args.parameters, 'r') as f:
        params = json.load(f)

    return run_gym(params, args.score_bar, args.gpu_id)


def run_gym(params, score_bar, gpu_id):
    rl_settings = params['rl']
    rl_settings['gamma'] = rl_settings['reward_discount_factor']
    del rl_settings['reward_discount_factor']

    env_type = params['env']
    env = OpenAIGymEnvironment(env_type, rl_settings['epsilon'])
    model_type = params['model_type']
    c2_device = core.DeviceOption(
        caffe2_pb2.CPU if gpu_id == USE_CPU else caffe2_pb2.CUDA,
        gpu_id,
    )

    if model_type == ModelType.DISCRETE_ACTION.value:
        with core.DeviceScope(c2_device):
            training_settings = params['training']
            training_settings['gamma'] = training_settings['learning_rate_decay']
            del training_settings['learning_rate_decay']
            trainer_params = DiscreteActionModelParameters(
                actions=env.actions,
                rl=RLParameters(**rl_settings),
                training=TrainingParameters(**training_settings)
            )
            if env.img:
                trainer = DiscreteActionConvTrainer(
                    DiscreteActionConvModelParameters(
                        fc_parameters=trainer_params,
                        cnn_parameters=CNNModelParameters(**params['cnn']),
                        num_input_channels=env.num_input_channels,
                        img_height=env.height,
                        img_width=env.width
                    ),
                    env.normalization,
                )
            else:
                trainer = DiscreteActionTrainer(
                    trainer_params,
                    env.normalization,
                )
    elif model_type == ModelType.PARAMETRIC_ACTION.value:
        with core.DeviceScope(c2_device):
            training_settings = params['training']
            training_settings['gamma'] = training_settings['learning_rate_decay']
            del training_settings['learning_rate_decay']
            trainer_params = ContinuousActionModelParameters(
                rl=RLParameters(**rl_settings),
                training=TrainingParameters(**training_settings),
                knn=KnnParameters(model_type='DQN', ),
            )
            trainer = ContinuousActionDQNTrainer(
                trainer_params,
                env.normalization,
                env.normalization_action
            )
    elif model_type == ModelType.CONTINUOUS_ACTION.value:
            training_settings = params['shared_training']
            training_settings['gamma'] = training_settings['learning_rate_decay']
            del training_settings['learning_rate_decay']
            actor_settings = params['actor_training']
            critic_settings = params['critic_training']
            trainer_params = DDPGModelParameters(
                rl=DDPGRLParameters(**rl_settings),
                shared_training=DDPGTrainingParameters(**training_settings),
                actor_training=DDPGNetworkParameters(**actor_settings),
                critic_training=DDPGNetworkParameters(**critic_settings),
            )
            trainer = DDPGTrainer(trainer_params, EnvDetails(
                state_dim=env.state_dim,
                action_dim=env.action_dim,
                action_range=(env.action_space.low, env.action_space.high),
            ))
    else:
        raise NotImplementedError(
            "Model of type {} not supported".format(model_type))

    return run(
        env, model_type, trainer, "{} test run".format(env_type), score_bar,
        **params["run_details"]
    )


if __name__ == '__main__':
    args = sys.argv
    if len(args) not in [3, 5, 7]:
        raise Exception(
            "Usage: python run_gym.py -p <parameters_file>" +
            " [-s <score_bar>] [-g <gpu_id>]"
        )
    main(args[1:])

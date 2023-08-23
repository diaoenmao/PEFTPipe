import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from transformers import get_linear_schedule_with_warmup
from config import cfg
from .huggingface import make_hf_model
from peft import get_peft_model, TaskType, LoraConfig, AdaLoraConfig, IA3Config, PromptTuningInit, \
    PromptTuningConfig, PrefixTuningConfig, PromptEncoderConfig


def make_model(model_name):
    model, tokenizer = make_hf_model(model_name)
    return model, tokenizer


def make_loss(output, input):
    if 'target' in input:
        loss = loss_fn(output['target'], input['target'])
    else:
        return
    return loss


def loss_fn(output, target, reduction='mean'):
    if target.dtype == torch.int64:
        loss = F.cross_entropy(output, target, reduction=reduction)
    else:
        loss = kld_loss(output, target, reduction=reduction)
    return loss


def cross_entropy_loss(output, target, reduction='mean'):
    if target.dtype != torch.int64:
        target = (target.topk(1, 1, True, True)[1]).view(-1)
    ce = F.cross_entropy(output, target, reduction=reduction)
    return ce


def kld_loss(output, target, reduction='batchmean'):
    kld = F.kl_div(F.log_softmax(output, dim=-1), target, reduction=reduction)
    return kld


def mse_loss(output, target, reduction='mean'):
    mse = F.mse_loss(output, target, reduction=reduction)
    return mse


def init_param(m):
    if isinstance(m, (nn.Conv2d, nn.ConvTranspose2d)):
        nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.BatchNorm2d):
        if m.weight is not None:
            m.weight.data.fill_(1)
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.Linear):
        if m.bias is not None:
            m.bias.data.zero_()
    return m


def make_batchnorm(m, momentum, track_running_stats):
    if isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d)):
        m.momentum = momentum
        m.track_running_stats = track_running_stats
        if track_running_stats:
            m.register_buffer('running_mean', torch.zeros(m.num_features, device=cfg['device']))
            m.register_buffer('running_var', torch.ones(m.num_features, device=cfg['device']))
            m.register_buffer('num_batches_tracked', torch.tensor(0, dtype=torch.long, device=cfg['device']))
        else:
            m.running_mean = None
            m.running_var = None
            m.num_batches_tracked = None
    return m


def make_optimizer(parameters, tag):
    if cfg[tag]['optimizer_name'] == 'SGD':
        optimizer = optim.SGD(parameters, lr=cfg[tag]['lr'], momentum=cfg[tag]['momentum'],
                              weight_decay=cfg[tag]['weight_decay'], nesterov=cfg[tag]['nesterov'])
    elif cfg[tag]['optimizer_name'] == 'Adam':
        optimizer = optim.Adam(parameters, lr=cfg[tag]['lr'], betas=cfg[tag]['betas'],
                               weight_decay=cfg[tag]['weight_decay'])
    elif cfg[tag]['optimizer_name'] == 'AdamW':
        optimizer = optim.AdamW(parameters, lr=cfg[tag]['lr'], betas=cfg[tag]['betas'],
                                weight_decay=cfg[tag]['weight_decay'])
    elif cfg[tag]['optimizer_name'] == 'LBFGS':
        optimizer = optim.LBFGS(parameters, lr=cfg[tag]['lr'])
    else:
        raise ValueError('Not valid optimizer name')
    return optimizer


def make_scheduler(optimizer, tag):
    if cfg[tag]['scheduler_name'] == 'None':
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[65535])
    elif cfg[tag]['scheduler_name'] == 'StepLR':
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=cfg[tag]['step_size'], gamma=cfg[tag]['factor'])
    elif cfg[tag]['scheduler_name'] == 'MultiStepLR':
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=cfg[tag]['milestones'],
                                                   gamma=cfg[tag]['factor'])
    elif cfg[tag]['scheduler_name'] == 'ExponentialLR':
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.99)
    elif cfg[tag]['scheduler_name'] == 'CosineAnnealingLR':
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=cfg[tag]['num_epochs'], eta_min=0)
    elif cfg[tag]['scheduler_name'] == 'ReduceLROnPlateau':
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=cfg[tag]['factor'],
                                                         patience=cfg[tag]['patience'], verbose=False,
                                                         threshold=cfg[tag]['threshold'], threshold_mode='rel',
                                                         min_lr=cfg[tag]['min_lr'])
    elif cfg[tag]['scheduler_name'] == 'CyclicLR':
        scheduler = optim.lr_scheduler.CyclicLR(optimizer, base_lr=cfg[tag]['lr'], max_lr=10 * cfg[tag]['lr'])
    elif cfg[tag]['scheduler_name'] == 'LinearAnnealingLR':
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0,
                                                    num_training_steps=cfg['num_steps']['train'] *
                                                                       cfg[cfg['model_name']]['num_epochs'])
    else:
        raise ValueError('Not valid scheduler name')
    return scheduler


def make_ft_model(model):
    if cfg['task_name'] == 'clm':
        peft_config = make_config_clm()
    elif cfg['task_name'] == 's2s':
        peft_config = make_config_s2s()
    elif cfg['task_name'] == 'sc':
        peft_config = make_config_sc()
    else:
        raise ValueError('Not valid task name')
    model = get_peft_model(model, peft_config)
    return model


def make_config_clm():
    if cfg['ft_name'] == 'lora':
        peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, r=8, lora_alpha=32,
                                 lora_dropout=0.1)
    elif cfg['ft_name'] == 'adalora':
        peft_config = AdaLoraConfig(
            init_r=12,
            target_r=8,
            beta1=0.85,
            beta2=0.85,
            # tinit=200,
            # tfinal=1000,
            deltaT=10,
            lora_alpha=32,
            lora_dropout=0.1,
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
        )
    elif cfg['ft_name'] == 'ia3':
        peft_config = IA3Config(task_type=TaskType.CAUSAL_LM, inference_mode=False, feedforward_modules=[])
    elif cfg['ft_name'] == 'promptune':
        peft_config = PromptTuningConfig(
            task_type=TaskType.CAUSAL_LM,
            prompt_tuning_init=PromptTuningInit.TEXT,
            num_virtual_tokens=20,
            prompt_tuning_init_text=" ",  # Classify if the tweet is a complaint or not:
            tokenizer_name_or_path=cfg['tokenizer_name_or_path'],
        )
    elif cfg['ft_name'] == 'prefixtune':
        peft_config = PrefixTuningConfig(task_type=TaskType.CAUSAL_LM, num_virtual_tokens=20)
    elif cfg['ft_name'] == 'ptune':
        peft_config = PromptEncoderConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, num_virtual_tokens=20,
                                          encoder_hidden_size=128)
    else:
        raise ValueError('Not valid ft name')
    return peft_config


def make_config_s2s():
    if cfg['ft_name'] == 'lora':
        peft_config = LoraConfig(
            task_type=TaskType.SEQ_2_SEQ_LM,
            r=64,
            lora_alpha=32,
            # target_modules=["q_proj", "v_proj"],
            lora_dropout=0.01,
            bias="none",
        )
    elif cfg['ft_name'] == 'adalora':
        peft_config = AdaLoraConfig(
            init_r=12,
            target_r=8,
            beta1=0.85,
            beta2=0.85,
            # tinit=200,
            # tfinal=1000,
            deltaT=10,
            lora_alpha=32,
            lora_dropout=0.1,
            task_type=TaskType.SEQ_2_SEQ_LM,
            inference_mode=False,
        )
    elif cfg['ft_name'] == 'ia3':
        peft_config = IA3Config(task_type=TaskType.SEQ_2_SEQ_LM, inference_mode=False, feedforward_modules=[])
    elif cfg['ft_name'] == 'promptune':
        peft_config = PromptTuningConfig(
            task_type=TaskType.SEQ_2_SEQ_LM,
            prompt_tuning_init=PromptTuningInit.TEXT,
            num_virtual_tokens=20,
            prompt_tuning_init_text=" ",  # "What is the sentiment of this article?\n"
            inference_mode=False,
            tokenizer_name_or_path=cfg['tokenizer_name_or_path'],
        )
    elif cfg['ft_name'] == 'prefixtune':
        peft_config = PrefixTuningConfig(task_type=TaskType.SEQ_2_SEQ_LM, inference_mode=False, num_virtual_tokens=20)
    elif cfg['ft_name'] == 'ptune':
        peft_config = PromptEncoderConfig(task_type=TaskType.SEQ_2_SEQ_LM, inference_mode=False, num_virtual_tokens=20,
                                          encoder_hidden_size=128)
    else:
        raise ValueError('Not valid ft name')
    return peft_config


def make_config_sc():
    if cfg['ft_name'] == 'lora':
        peft_config = LoraConfig(
            task_type=TaskType.SEQ_CLS,
            r=64,
            lora_alpha=32,
            # target_modules=["q_proj", "v_proj"],
            lora_dropout=0.01,
            bias="none",
        )
    elif cfg['ft_name'] == 'adalora':
        peft_config = AdaLoraConfig(
            init_r=12,
            target_r=8,
            beta1=0.85,
            beta2=0.85,
            # tinit=200,
            # tfinal=1000,
            deltaT=10,
            lora_alpha=32,
            lora_dropout=0.1,
            task_type=TaskType.SEQ_CLS,
            inference_mode=False,
        )
    elif cfg['ft_name'] == 'ia3':
        peft_config = IA3Config(task_type=TaskType.SEQ_CLS, inference_mode=False, feedforward_modules=[])
    elif cfg['ft_name'] == 'promptune':
        peft_config = PromptTuningConfig(
            task_type=TaskType.SEQ_CLS,
            prompt_tuning_init=PromptTuningInit.TEXT,
            num_virtual_tokens=20,
            prompt_tuning_init_text=" ",  # "What is the sentiment of this article?\n"
            inference_mode=False,
            tokenizer_name_or_path=cfg['tokenizer_name_or_path'],
        )
    elif cfg['ft_name'] == 'prefixtune':
        peft_config = PrefixTuningConfig(task_type=TaskType.SEQ_CLS, inference_mode=False, num_virtual_tokens=20)
    elif cfg['ft_name'] == 'ptune':
        peft_config = PromptEncoderConfig(task_type=TaskType.SEQ_CLS, inference_mode=False, num_virtual_tokens=20,
                                          encoder_hidden_size=128)
    else:
        raise ValueError('Not valid ft name')
    return peft_config
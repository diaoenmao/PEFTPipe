import argparse
import os
import torch
import torch.backends.cudnn as cudnn
from config import cfg, process_args
from model import make_model
from module import save, makedir_exist_ok, process_control, resume

cudnn.benchmark = True
parser = argparse.ArgumentParser(description='cfg')
for k in cfg:
    exec('parser.add_argument(\'--{0}\', default=cfg[\'{0}\'], type=type(cfg[\'{0}\']))'.format(k))
parser.add_argument('--control_name', default=None, type=str)
args = vars(parser.parse_args())
process_args(args)


def main():
    process_control()
    seeds = list(range(cfg['init_seed'], cfg['init_seed'] + cfg['num_experiments']))
    for i in range(cfg['num_experiments']):
        model_tag_list = [str(seeds[i]), cfg['control_name']]
        cfg['model_tag'] = '_'.join([x for x in model_tag_list if x])
        print('Experiment: {}'.format(cfg['model_tag']))
        runExperiment()
    return


def runExperiment():
    output_format = 'png'
    num_generated = 30
    cfg['seed'] = int(cfg['model_tag'].split('_')[0])
    torch.manual_seed(cfg['seed'])
    torch.cuda.manual_seed(cfg['seed'])
    model_path = os.path.join('output', 'model')
    result_path = os.path.join('output', 'result')
    model_tag_path = os.path.join(model_path, cfg['model_tag'])
    best_path = os.path.join(model_tag_path, 'best')
    model, tokenizer = make_model(cfg['model_name'])
    result = resume(os.path.join(best_path, 'model'))
    model.unet.load_state_dict(result['model_state_dict'])
    model = model.to(cfg['device'])
    generate_dir = os.path.join(result_path, cfg['model_tag'])
    makedir_exist_ok(generate_dir)
    with torch.no_grad():
        model.vae.train(False)
        model.unet.train(False)
        model.text_encoder.train(False)
        for i in range(num_generated):
            INSTANCE_PROMPT = f"a photo of {cfg['unique_id']} {cfg['unique_class']}"
            image = model(INSTANCE_PROMPT, num_inference_steps=cfg[cfg['model_name']]['num_inference_steps'], \
                          guidance_scale=cfg[cfg['model_name']]['guidance_scale']).images[0]
            # Convert to RGB if your model outputs RGBA format, as PDF doesn't support RGBA
            if image.mode == 'RGBA':
                image = image.convert('RGB')
            image_path = os.path.join(generate_dir, f"{i}.{output_format}")
            # Save as PDF
            image.save(image_path, output_format.upper(), resolution=100.0)
    return


if __name__ == "__main__":
    main()

import argparse
import logging
import math
import os
import random
import sys

from collections import Counter

import numpy as np
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from IPython import embed

import options as option
from models import create_model

sys.path.insert(0, "../../")
import utils as util
from data import create_dataloader, create_dataset
from data.data_sampler import DistIterSampler
import subprocess


# def init_dist(backend="nccl", **kwargs):
#     """ initialization for distributed training"""
#     # if mp.get_start_method(allow_none=True) is None:
#     if (
#         mp.get_start_method(allow_none=True) != "spawn"
#     ):  # Return the name of start method used for starting processes
#         mp.set_start_method("spawn", force=True)  ##'spawn' is the default on Windows
#     rank = int(os.environ["RANK"])  # system env process ranks
#     print(torch.cuda.is_available())
#     print(torch.cuda.is_available())
#     print(torch.cuda.is_available())
#     num_gpus = torch.cuda.device_count()  # Returns the number of GPUs available
#     print("gpus:%d" % num_gpus)
#     print("gpus:%d" % num_gpus)
#     print("gpus:%d" % num_gpus)
#     torch.cuda.set_device(rank % num_gpus)
#     dist.init_process_group(
#         backend=backend, **kwargs
#     )  # Initializes the default distributed process group


def init_dist(backend="nccl", port=None):
    """Initialize distributed training environment.
    support both slurm and torch.distributed.launch
    see torch.distributed.init_process_group() for more details
    """
    if mp.get_start_method(allow_none=True) is None:
        if (
            mp.get_start_method(allow_none=True) != "spawn"
        ):  # Return the name of start method used for starting processes
            mp.set_start_method("spawn", force=True)  ##'spawn' is the default on Windows
    num_gpus = torch.cuda.device_count()

    if "SLURM_JOB_ID" in os.environ:
        rank = int(os.environ["SLURM_PROCID"])
        world_size = int(os.environ["SLURM_NTASKS"])
        node_list = os.environ["SLURM_NODELIST"]
        print("当前有多少结点：%s" % node_list)
        print("当前有多少结点：%s" % node_list)
        addr = subprocess.getoutput(f"scontrol show hostname {node_list} | head -n1")
        print("当前地址为：%s" % str(addr))
        print("当前地址为：%s" % str(addr))
        # specify master port
        if port is not None:
            os.environ["MASTER_PORT"] = str(port)
        elif "MASTER_PORT" not in os.environ:
            os.environ["MASTER_PORT"] = "29500"
        if "MASTER_ADDR" not in os.environ:
            os.environ["MASTER_ADDR"] = addr
        os.environ["WORLD_SIZE"] = str(world_size)
        os.environ["LOCAL_RANK"] = str(rank % num_gpus)
        os.environ["RANK"] = str(rank)
    else:
        rank = int(os.environ["RANK"])
        world_size = int(os.environ["WORLD_SIZE"])

    print("当前可用gpu个数为：%d" % num_gpus)
    print("当前可用gpu个数为：%d" % num_gpus)
    print("当前rank为：%d" % rank)
    print("当前rank为：%d" % rank)
    print("world_size为：%d" % world_size)
    print("world_size为：%d" % world_size)
    torch.cuda.set_device(rank % num_gpus)

    dist.init_process_group(
        backend=backend,
        world_size=world_size,
        rank=rank,
    )


def main():
    #### setup options of three networks
    parser = argparse.ArgumentParser()
    parser.add_argument("-opt", type=str, help="Path to option YMAL file.")
    parser.add_argument(
        "--launcher", choices=["none", "pytorch"], default="none", help="job launcher"
    )
    parser.add_argument("--local_rank", type=int, default=0)
    args = parser.parse_args()
    opt = option.parse(args.opt, is_train=True)

    # convert to NoneDict, which returns None for missing keys
    opt = option.dict_to_nonedict(opt)

    # choose small opt for SFTMD test, fill path of pre-trained model_F
    #### set random seed
    seed = opt["train"]["manual_seed"]
    if seed is None:
        seed = random.randint(1, 10000)
    util.set_random_seed(seed)

    # load PCA matrix of enough kernel
    print("load PCA matrix")
    pca_matrix = torch.load(
        opt["pca_matrix_path"], map_location=lambda storage, loc: storage
    )
    print("PCA matrix shape: {}".format(pca_matrix.shape))


    #### distributed training settings
    if args.launcher == "none":  # disabled distributed training
        opt["dist"] = False
        opt["dist"] = False
        rank = -1
        print("Disabled distributed training.")
    else:
        opt["dist"] = True
        opt["dist"] = True
        init_dist()
        world_size = (
            torch.distributed.get_world_size()
        )  # Returns the number of processes in the current process group
        rank = torch.distributed.get_rank()  # Returns the rank of current process group

    torch.backends.cudnn.benchmark = True
    # torch.backends.cudnn.deterministic = True

    ###### Predictor&Corrector train ######

    #### loading resume state if exists
    if opt["path"].get("resume_state", None):
        # distributed resuming: all load into default GPU
        device_id = torch.cuda.current_device()
        resume_state = torch.load(
            opt["path"]["resume_state"],
            map_location=lambda storage, loc: storage.cuda(device_id),
        )
        option.check_resume(opt, resume_state["iter"])  # check resume options
        # iters为300000时，学习率衰减一次；iters为500000时，学习率衰减第二次....
        resume_state["schedulers"][0]['milestones'] = Counter({300000: 1, 500000: 2, 700000: 3, 900000: 4})
        print(resume_state["schedulers"][0])
    else:
        resume_state = None

    #### mkdir and loggers
    if rank <= 0:  # normal training (rank -1) OR distributed training (rank 0-7)
        if resume_state is None:
            # Predictor path
            util.mkdir_and_rename(
                opt["path"]["experiments_root"]
            )  # rename experiment folder if exists
            util.mkdirs(
                (
                    path
                    for key, path in opt["path"].items()
                    if not key == "experiments_root"
                    and "pretrain_model" not in key
                    and "resume" not in key
                )
            )
            os.system("rm ./log")
            os.symlink(os.path.join(opt["path"]["experiments_root"], ".."), "./log")

        # config loggers. Before it, the log will not work
        util.setup_logger(
            "base",
            opt["path"]["log"],
            "train_" + opt["name"],
            level=logging.INFO,
            screen=False,
            tofile=True,
        )
        util.setup_logger(
            "val",
            opt["path"]["log"],
            "val_" + opt["name"],
            level=logging.INFO,
            screen=False,
            tofile=True,
        )
        logger = logging.getLogger("base")
        logger.info(option.dict2str(opt))
        # tensorboard logger
        if opt["use_tb_logger"] and "debug" not in opt["name"]:
            version = float(torch.__version__[0:3])
            if version >= 1.1:  # PyTorch 1.1
                from torch.utils.tensorboard import SummaryWriter
            else:
                logger.info(
                    "You are using PyTorch {}. Tensorboard will use [tensorboardX]".format(
                        version
                    )
                )
                from tensorboardX import SummaryWriter
            tb_logger = SummaryWriter(log_dir="log/{}/tb_logger/".format(opt["name"]))
    else:
        util.setup_logger(
            "base", opt["path"]["log"], "train", level=logging.INFO, screen=False
        )
        logger = logging.getLogger("base")

    torch.backends.cudnn.benchmark = True
    # torch.backends.cudnn.deterministic = True

    #### create train and val dataloader
    dataset_ratio = 200  # enlarge the size of each epoch
    for phase, dataset_opt in opt["datasets"].items():
        if phase == "train":
            train_set = create_dataset(dataset_opt)
            train_size = int(math.ceil(len(train_set) / dataset_opt["batch_size"]))
            total_iters = int(opt["train"]["niter"])
            total_epochs = int(math.ceil(total_iters / train_size))
            if opt["dist"]:
                train_sampler = DistIterSampler(
                    train_set, world_size, rank, dataset_ratio
                )
                total_epochs = int(
                    math.ceil(total_iters / (train_size * dataset_ratio))
                )
            else:
                train_sampler = None
            train_loader = create_dataloader(train_set, dataset_opt, opt, train_sampler)
            if rank <= 0:
                logger.info(
                    "Number of train images: {:,d}, iters: {:,d}".format(
                        len(train_set), train_size
                    )
                )
                logger.info(
                    "Total epochs needed: {:d} for iters {:,d}".format(
                        total_epochs, total_iters
                    )
                )
        elif phase == "val":
            val_set = create_dataset(dataset_opt)
            val_loader = create_dataloader(val_set, dataset_opt, opt, None)
            if rank <= 0:
                logger.info(
                    "Number of val images in [{:s}]: {:d}".format(
                        dataset_opt["name"], len(val_set)
                    )
                )
        else:
            raise NotImplementedError("Phase [{:s}] is not recognized.".format(phase))
    assert train_loader is not None
    assert val_loader is not None

    #### create model
    model = create_model(opt)  # load pretrained model of SFTMD

    #### resume training
    if resume_state:
        logger.info(
            "Resuming training from epoch: {}, iter: {}.".format(
                resume_state["epoch"], resume_state["iter"]
            )
        )

        start_epoch = resume_state["epoch"]
        current_step = resume_state["iter"]
        model.resume_training(resume_state)  # handle optimizers and schedulers
    else:
        current_step = 0
        start_epoch = 0

    prepro = util.SRMDPreprocessing(
        scale=opt["scale"], pca_matrix=pca_matrix, cuda=True, **opt["degradation"]
    )
    #### training
    logger.info(
        "Start training from epoch: {:d}, iter: {:d}".format(start_epoch, current_step)
    )
    for epoch in range(start_epoch, total_epochs + 1):
        if opt["dist"]:
            train_sampler.set_epoch(epoch)
        for _, train_data in enumerate(train_loader):
            torch.cuda.empty_cache()
            print("当前epoch为：%d，当前iter为%d" % (epoch, current_step))
            current_step += 1

            if current_step > total_iters:
                break

            LR_img, ker_map = prepro(train_data["GT"])
            LR_img = (LR_img * 255).round() / 255

            model.feed_data(LR_img, train_data["GT"], ker_map)
            model.optimize_parameters(current_step)
            model.update_learning_rate(
                current_step, warmup_iter=opt["train"]["warmup_iter"]
            )
            visuals = model.get_current_visuals()

            if current_step % opt["logger"]["print_freq"] == 0:
                logs = model.get_current_log()
                message = "<epoch:{:3d}, iter:{:8,d}, lr:{:.3e}> ".format(
                    epoch, current_step, model.get_current_learning_rate()
                )
                for k, v in logs.items():
                    message += "{:s}: {:.4e} ".format(k, v)
                    # tensorboard logger
                    if opt["use_tb_logger"] and "debug" not in opt["name"]:
                        if rank <= 0:
                            tb_logger.add_scalar(k, v, current_step)
                if rank == 0:
                    logger.info(message)

            # validation, to produce ker_map_list(fake)
            if current_step % opt["train"]["val_freq"] == 0 and rank <= 0:
                avg_psnr = 0.0
                idx = 0
                for _, val_data in enumerate(val_loader):
                    torch.cuda.empty_cache()

                    # LR_img, ker_map = prepro(val_data['GT'])
                    LR_img = val_data["LQ"]
                    lr_img = util.tensor2img(LR_img)  # save LR image for reference

                    # valid Predictor
                    model.feed_data(LR_img, val_data["GT"])
                    model.test()
                    visuals = model.get_current_visuals()

                    # Save images for reference
                    img_name = os.path.splitext(
                        os.path.basename(val_data["LQ_path"][0])
                    )[0]
                    img_dir = os.path.join(opt["path"]["val_images"], img_name)
                    # img_dir = os.path.join(opt['path']['val_images'], str(current_step), '_', str(step))
                    util.mkdir(img_dir)
                    save_lr_path = os.path.join(img_dir, "{:s}_LR.png".format(img_name))
                    util.save_img(lr_img, save_lr_path)

                    sr_img = util.tensor2img(visuals["SR"])  # uint8
                    gt_img = util.tensor2img(visuals["GT"])  # uint8

                    save_img_path = os.path.join(
                        img_dir, "{:s}_{:d}.png".format(img_name, current_step)
                    )
                    util.save_img(sr_img, save_img_path)

                    # calculate PSNR
                    crop_size = opt["scale"]
                    gt_img = gt_img / 255.0
                    sr_img = sr_img / 255.0
                    cropped_sr_img = sr_img[
                        crop_size:-crop_size, crop_size:-crop_size, :
                    ]
                    cropped_gt_img = gt_img[
                        crop_size:-crop_size, crop_size:-crop_size, :
                    ]

                    avg_psnr += util.calculate_psnr(
                        cropped_sr_img * 255, cropped_gt_img * 255
                    )
                    idx += 1

                avg_psnr = avg_psnr / idx
                print("当前psnr为：%f" % avg_psnr)
                print("当前psnr为：%f" % avg_psnr)
                print("当前psnr为：%f" % avg_psnr)
                print("当前psnr为：%f" % avg_psnr)
                print("当前psnr为：%f" % avg_psnr)

                # log
                logger.info("# Validation # PSNR: {:.6f}".format(avg_psnr))
                logger_val = logging.getLogger("val")  # validation logger
                logger_val.info(
                    "<epoch:{:3d}, iter:{:8,d}, psnr: {:.6f}".format(
                        epoch, current_step, avg_psnr
                    )
                )
                # tensorboard logger
                if opt["use_tb_logger"] and "debug" not in opt["name"]:
                    tb_logger.add_scalar("psnr", avg_psnr, current_step)

            #### save models and training states
            if current_step % opt["logger"]["save_checkpoint_freq"] == 0:
                if rank <= 0:
                    logger.info("Saving models and training states.")
                    model.save(current_step)
                    model.save_training_state(epoch, current_step)

    if rank <= 0:
        logger.info("Saving the final model.")
        model.save("latest")
        logger.info("End of Predictor and Corrector training.")
    tb_logger.close()


if __name__ == "__main__":
    torch.cuda.empty_cache()
    main()

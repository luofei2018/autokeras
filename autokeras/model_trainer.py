import abc
import sys
from copy import deepcopy
from functools import reduce
import numpy as np
import torch
from torchvision import utils as vutils
from tqdm import trange

from autokeras.constant import Constant
from autokeras.utils import EarlyStop, get_device


class ModelTrainerBase(abc.ABC):
    def __init__(self,
                 loss_function,
                 train_data,
                 test_data=None,
                 metric=None,
                 verbose=False):
        self.device = get_device()
        self.metric = metric
        self.verbose = verbose
        self.loss_function = loss_function
        self.train_loader = train_data
        self.test_loader = test_data

    @abc.abstractmethod
    def train_model(self,
                    max_iter_num=Constant.MAX_ITER_NUM,
                    max_no_improvement_num=Constant.MAX_NO_IMPROVEMENT_NUM):
        pass


class ModelTrainer(ModelTrainerBase):
    """A class that is used to train the model.

    This class can train a Pytorch model with the given data loaders.
    The metric, loss_function, and model must be compatible with each other.
    Please see the details in the Attributes.

    Attributes:
        device: A string. Indicating the device to use. 'cuda' or 'cpu'.
        model: An instance of Pytorch Module. The model that will be trained.
        train_loader: Training data wrapped in batches in Pytorch Dataloader.
        test_loader: Testing data wrapped in batches in Pytorch Dataloader.
        loss_function: A function with two parameters (prediction, target).
            There is no specific requirement for the types of the parameters,
            as long as they are compatible with the model and the data loaders.
            The prediction should be the output of the model for a batch.
            The target should be a batch of targets packed in the data loaders.
        optimizer: The optimizer is chosen to use the Pytorch Adam optimizer.
        early_stop: An instance of class EarlyStop.
        metric: It should be a subclass of class autokeras.metric.Metric.
            In the compute(prediction, target) function, prediction and targets are
            all numpy arrays converted from the output of the model and the targets packed in the data loaders.
        verbose: Verbosity mode.
    """

    def __init__(self, model, **kwargs):
        """Init the ModelTrainer with `model`, `x_train`, `y_train`, `x_test`, `y_test`, `verbose`"""
        super().__init__(**kwargs)
        self.model = model
        self.model.to(self.device)
        self.optimizer = None
        self.early_stop = None
        self.current_epoch = 0
        self.current_metric_value = 0

    def train_model(self,
                    max_iter_num=None,
                    max_no_improvement_num=None):
        """Train the model.

        Args:
            max_iter_num: An integer. The maximum number of epochs to train the model.
                The training will stop when this number is reached.
            max_no_improvement_num: An integer. The maximum number of epochs when the loss value doesn't decrease.
                The training will stop when this number is reached.
        """
        if max_iter_num is None:
            max_iter_num = Constant.MAX_ITER_NUM

        if max_no_improvement_num is None:
            max_no_improvement_num = Constant.MAX_NO_IMPROVEMENT_NUM

        self.early_stop = EarlyStop(max_no_improvement_num)
        self.early_stop.on_train_begin()

        test_metric_value_list = []
        test_loss_list = []
        self.optimizer = torch.optim.Adam(self.model.parameters())

        for epoch in range(max_iter_num):
            self._train()
            test_loss, metric_value = self._test()
            self.current_metric_value = metric_value
            test_metric_value_list.append(metric_value)
            test_loss_list.append(test_loss)
            decreasing = self.early_stop.on_epoch_end(test_loss)
            if not decreasing:
                if self.verbose:
                    print('\nNo loss decrease after {} epochs.\n'.format(max_no_improvement_num))
                break
        last_num = min(max_no_improvement_num, max_iter_num)
        return (sum(test_loss_list[-last_num:]) / last_num,
                sum(test_metric_value_list[-last_num:]) / last_num)

    def _train(self):
        self.model.train()
        loader = self.train_loader
        self.current_epoch += 1

        cp_loader = deepcopy(loader)

        for batch_idx, (inputs, targets) in enumerate(cp_loader):
            inputs, targets = inputs.to(self.device), targets.to(self.device)
            self.optimizer.zero_grad()
            outputs = self.model(inputs)
            loss = self.loss_function(outputs, targets)
            loss.backward()
            self.optimizer.step()

    def _test(self):
        self.model.eval()
        test_loss = 0
        all_targets = []
        all_predicted = []
        loader = self.test_loader
        with torch.no_grad():
            for batch_idx, (inputs, targets) in enumerate(deepcopy(loader)):
                inputs, targets = inputs.to(self.device), targets.to(self.device)
                outputs = self.model(inputs)
                # cast tensor to float
                test_loss += float(self.loss_function(outputs, targets))

                all_predicted.append(outputs.cpu().numpy())
                all_targets.append(targets.cpu().numpy())
        all_predicted = reduce(lambda x, y: np.concatenate((x, y)), all_predicted)
        all_targets = reduce(lambda x, y: np.concatenate((x, y)), all_targets)
        return test_loss, self.metric.compute(all_predicted, all_targets)


class GANModelTrainer(ModelTrainerBase):
    def __init__(self,
                 g_model,
                 d_model,
                 train_data,
                 loss_function,
                 verbose,
                 gen_training_result=None):
        """Init the ModelTrainer with `model`, `x_train`, `y_train`, `x_test`, `y_test`, `verbose`"""
        super().__init__(loss_function, train_data, verbose=verbose)
        self.d_model = d_model
        self.g_model = g_model
        self.d_model.to(self.device)
        self.g_model.to(self.device)
        self.outf = None
        self.out_size = 0
        if gen_training_result is not None:
            self.outf, self.out_size = gen_training_result
            self.sample_noise = torch.randn(self.out_size,
                                            self.g_model.nz,
                                            1, 1, device=self.device)
        self.optimizer_d = None
        self.optimizer_g = None

    def train_model(self,
                    max_iter_num=Constant.MAX_ITER_NUM,
                    max_no_improvement_num=Constant.MAX_NO_IMPROVEMENT_NUM):
        self.optimizer_d = torch.optim.Adam(self.d_model.parameters())
        self.optimizer_g = torch.optim.Adam(self.g_model.parameters())
        for epoch in range(max_iter_num):
            self._train(epoch)

    def _train(self, epoch):
        # put model into train mode
        self.d_model.train()
        # TODO: why?
        cp_loader = deepcopy(self.train_loader)

        real_label = 1
        fake_label = 0
        for batch_idx, inputs in enumerate(cp_loader):
            # Update Discriminator network maximize log(D(x)) + log(1 - D(G(z)))
            # train with real
            self.optimizer_d.zero_grad()
            inputs = inputs.to(self.device)
            batch_size = inputs.size(0)
            outputs = self.d_model(inputs)

            label = torch.full((batch_size,), real_label, device=self.device)
            loss_d_real = self.loss_function(outputs, label)
            loss_d_real.backward()

            # train with fake
            noise = torch.randn((batch_size, self.g_model.nz, 1, 1,), device=self.device)
            fake_outputs = self.g_model(noise)
            label.fill_(fake_label)
            outputs = self.d_model(fake_outputs.detach())
            loss_g_fake = self.loss_function(outputs, label)
            loss_g_fake.backward()
            self.optimizer_d.step()
            # (2) Update G network: maximize log(D(G(z)))
            self.g_model.zero_grad()
            label.fill_(real_label)
            outputs = self.d_model(fake_outputs)
            loss_g = self.loss_function(outputs, label)
            loss_g.backward()
            self.optimizer_g.step()

            if self.outf is not None and batch_idx % 100 == 0:
                fake = self.g_model(self.sample_noise)
                vutils.save_image(
                    fake.detach(),
                    '%s/fake_samples_epoch_%03d.png' % (self.outf, epoch),
                    normalize=True)

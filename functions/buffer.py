
import torch
import numpy as np
from typing import Tuple
from torch.functional import Tensor
from torchvision import transforms
from torch.utils.data import Dataset
import gc
from functions.create_partition import create_partition_func_1nn, create_partition_func_grid
from functions.no_bn import bn_track_stats


def reservoir(num_seen_examples: int, buffer_size: int) -> int:
    """
    Reservoir sampling algorithm.
    :param num_seen_examples: the number of seen examples
    :param buffer_size: the maximum buffer size
    :return: the target index if the current image is sampled, else -1
    """
    if num_seen_examples < buffer_size:
        return num_seen_examples

    rand = np.random.randint(0, num_seen_examples + 1)
    if rand < buffer_size:
        return rand
    else:
        return -1


def ring(num_seen_examples: int, buffer_portion_size: int, task: int) -> int:
    return num_seen_examples % buffer_portion_size + task * buffer_portion_size


class Buffer(Dataset):
    """
    The memory buffer of rehearsal method.
    """
    def __init__(self, buffer_size, device, n_tasks=None, mode='reservoir'):
        assert mode in ['ring', 'reservoir']
        self.buffer_size = buffer_size
        self.device = device
        self.num_seen_examples = 0
        self.functional_index = eval(mode)
        if mode == 'ring':
            assert n_tasks is not None
            self.task_number = n_tasks
            self.buffer_portion_size = buffer_size // n_tasks
        self.attributes = ['examples', 'labels', 'logits', 'clusterID', 'task_labels']
        self.attention_maps = [None] * buffer_size
        self.lip_values = [None] * buffer_size

        self.balanced_class_perm  = None
        self.transform = None


    def class_stratified_add_data(self, dataset, cpt, model=None, desired_attrs=None):
        if not hasattr(self, 'task'):
            self.task = 0
        # Reduce Memory Buffer
        if self.task:
            examples_per_class = self.buffer_size // ((self.task + 1) * cpt)
            assert set(desired_attrs) == {x for x in self.attributes if hasattr(self, x)}
            ret_tuples = self.get_all_data()
            self.empty()
            for tl in ret_tuples[1].unique():
                idx = tl == ret_tuples[1]
                ret_tuple = [a[idx] for a in ret_tuples]
                first = min(ret_tuple[0].shape[0], examples_per_class)
                self.add_data(**{a: ret_tuple[i][:first] for i, a in enumerate(
                    [x for x in self.attributes if x in desired_attrs])})
        
        # Add new task data
        examples_last_task = self.buffer_size - self.num_seen_examples
        examples_per_class = examples_last_task // cpt
        ce = torch.tensor([examples_per_class] * cpt).int()
        ce[torch.randperm(cpt)[:examples_last_task - (examples_per_class * cpt)]] += 1

        with torch.no_grad():
            with bn_track_stats(model, False):
                for data in dataset.train_loader:
                    inputs, labels, not_aug_inputs = data
                    inputs = inputs.to(self.device)
                    not_aug_inputs = not_aug_inputs.to(self.device)
                    if all(ce == 0):
                        break

                    flags = torch.zeros(len(inputs)).bool()
                    for j in range(len(flags)):
                        if ce[labels[j] % cpt] > 0:
                            flags[j] = True
                            ce[labels[j] % cpt] -= 1

                    add_dict = {
                        'examples': not_aug_inputs[flags]
                    }
                    if hasattr(self, 'labels') or desired_attrs is not None and 'labels' in desired_attrs:
                        add_dict['labels'] = labels[flags]
                    if hasattr(self, 'logits') or desired_attrs is not None and 'logits' in desired_attrs:
                        outputs = model(inputs)
                        add_dict['logits'] = outputs.data[flags]
                    if hasattr(self, 'task_labels') or desired_attrs is not None and 'task_labels' in desired_attrs:
                        add_dict['task_labels'] = (torch.ones(len(not_aug_inputs)) *
                                                    (self.task))[flags]
                    if hasattr[self, 'clusterID'] or desired_attrs is not None and 'clusterID' in desired_attrs:
                        partition_func = create_partition_func_1nn((84, 84, 3), n_centroids=5000)
                        add_dict['clusterID'] = partition_func(not_aug_inputs[flags])
                    self.add_data(**add_dict)
        self.task += 1

    def generate_class_perm(self):
        self.balanced_class_perm = (self.labels.unique()[torch.randperm(len(self.labels.unique()))]).cpu()
        self.balanced_class_index = 0

    def to(self, device):
        self.device = device
        for attr_str in self.attributes:
            if hasattr(self, attr_str):
                setattr(self, attr_str, getattr(self, attr_str).to(device))

        return self

    def __len__(self):
        return min(self.num_seen_examples, self.buffer_size)

    def set_transform(self, transform):
        self.transform = transform

    def __getitem__(self, index: int) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        if self.transform is None:
            transform = lambda x: x
        else:
            transform = self.transform
        gc.collect(generation = 2)
        inp = self.examples[index]
        ret_tuple = (transform(inp).to(self.device), inp)
        for attr_str in self.attributes[1:]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str).to(self.device)
                ret_tuple += (attr[index],)

        return ret_tuple


    def init_tensors(self, examples: torch.Tensor, labels: torch.Tensor,
                     logits: torch.Tensor, clusterID: torch.Tensor ,task_labels: torch.Tensor) -> None:
        """
        Initializes just the required tensors.
        :param examples: tensor containing the images
        :param labels: tensor containing the labels
        :param logits: tensor containing the outputs of the network
        :param task_labels: tensor containing the task labels
        """
        for attr_str in self.attributes:
            attr = eval(attr_str)
            if attr is not None and not hasattr(self, attr_str):
                typ = torch.int64 if attr_str.endswith('els') else torch.float32
                setattr(self, attr_str, torch.zeros((self.buffer_size,
                        *attr.shape[1:]), dtype=typ, device=self.device))

    def add_data(self, examples, labels=None, logits=None, task_labels=None, clusterID = None, attention_maps=None, lip_values=None):
        """
        Adds the data to the memory buffer according to the reservoir strategy.
        :param examples: tensor containing the images
        :param labels: tensor containing the labels
        :param logits: tensor containing the outputs of the network
        :param task_labels: tensor containing the task labels
        :return: List of indices where the data was added
        """
        if not hasattr(self, 'examples'):
            self.init_tensors(examples, labels, logits, clusterID, task_labels)

        rix = []

        for i in range(examples.shape[0]):
            index = reservoir(self.num_seen_examples, self.buffer_size)
            self.num_seen_examples += 1

            if index >= 0:
                if self.examples.device != self.device:
                    self.examples.to(self.device)
                self.examples[index] = examples[i].to(self.device)
                
                if labels is not None:
                    if self.labels.device != self.device:
                        self.labels.to(self.device)
                    self.labels[index] = labels[i].to(self.device)

                if logits is not None:
                    if self.logits.device != self.device:
                        self.logits.to(self.device)
                    self.logits[index] = logits[i].to(self.device)

                if task_labels is not None:
                    if self.task_labels.device != self.device:
                        self.task_labels.to(self.device)
                    self.task_labels[index] = task_labels[i].to(self.device)

                if clusterID is not None:
                    if self.clusterID.device != self.device:
                        self.clusterID.to(self.device)
                    self.clusterID[index] = clusterID[i].to(self.device)

                if attention_maps is not None:
                    self.attention_maps[index] = [at[i].byte() for at in attention_maps]

                if lip_values is not None:
                    self.lip_values[index] = [val[i].data for val in lip_values]

            rix.append(index)
        return torch.tensor(rix).to(self.device)

    def get_data(self, size: int, transform: transforms=None, return_index=False, to_device=None) -> Tuple:
        """
        Random samples a batch of size items.
        :param size: the number of requested items
        :param transform: the transformation to be applied (data augmentation)
        :return:
        """
        if size > min(self.num_seen_examples, self.examples.shape[0]):
            size = min(self.num_seen_examples, self.examples.shape[0])

        target_device = self.device if to_device is None else to_device

        choice = np.random.choice(min(self.num_seen_examples, self.examples.shape[0]),
                                  size=size, replace=False)
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples[choice]]).to(target_device),)
        for attr_str in self.attributes[1:]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str).to(target_device)
                ret_tuple += (attr[choice],)

        if not return_index:
          return ret_tuple
        else:
          return (torch.tensor(choice).to(target_device), ) + ret_tuple

    def get_data_by_index(self, indexes: Tensor, transform: transforms=None, to_device=None) -> Tuple:
        """
        Returns the data by the given index.
        :param index: the index of the item
        :param transform: the transformation to be applied (data augmentation)
        :return:
        """
        target_device = self.device if to_device is None else to_device
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples[indexes]]).to(target_device),)
        for attr_str in self.attributes[:-1]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str).to(target_device)
                ret_tuple += (attr[indexes],)
        return ret_tuple


    def get_data_balanced(self, n_classes: int, n_instances: int, transform: transforms=None, return_index=False) -> Tuple:
        """
        Random samples a batch of size items.
        :param n_classes: the number of classes to sample
        :param n_instances: the number of instances to be sampled per class
        :param transform: the transformation to be applied (data augmentation)
        :return:
        """
        classes_to_sample = torch.tensor([])
        choice = torch.tensor([]).long()

        while len(classes_to_sample) < n_classes:
            if self.balanced_class_perm is None or \
               self.balanced_class_index >= len(self.balanced_class_perm) or \
               len(self.balanced_class_perm.unique()) != len(self.labels.unique()):
                self.generate_class_perm()
            
            classes_to_sample = torch.cat([
                classes_to_sample,
                self.balanced_class_perm[self.balanced_class_index:self.balanced_class_index+n_classes]
                ])
            self.balanced_class_index += n_classes

        for a_class in classes_to_sample:
            candidates = np.arange(len(self.labels))[self.labels.cpu() == a_class]
            candidates = candidates[candidates < self.num_seen_examples]
            choice = torch.cat([
                choice, 
                torch.tensor(
                    np.random.choice(candidates,
                    size=n_instances,
                    replace=len(candidates) < n_instances
                    )
                )
            ])
        
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples[choice]]).to(self.device),)
        for attr_str in self.attributes[1:]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str)
                ret_tuple += (attr[choice],)

        if not return_index:
          return ret_tuple
        else:
          return (choice.to(self.device), ) + ret_tuple

    def is_empty(self) -> bool:
        """
        Returns true if the buffer is empty, false otherwise.
        """
        if self.num_seen_examples == 0:
            return True
        else:
            return False

    def get_all_data(self, transform: transforms=None) -> Tuple:
        """
        Return all the items in the memory buffer.
        :param transform: the transformation to be applied (data augmentation)
        :return: a tuple with all the items in the memory buffer
        """
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples]).to(self.device),)
        for attr_str in self.attributes[1:4]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str)
                ret_tuple += (attr,)
        return ret_tuple

    def empty(self) -> None:
        """
        Set all the tensors to None.
        """
        for attr_str in self.attributes:
            if hasattr(self, attr_str):
                delattr(self, attr_str)
        self.num_seen_examples = 0
    
    def get_data_by_clusterID(self, clusterID, transform: transforms = None, return_index=False):
        """
        Returns data based on the provided clusterID.
        :param clusterID: the clusterID to filter the data
        :param transform: the transformation to be applied (data augmentation)
        :param return_index: if True, return indices along with data
        :return: Tuple of data based on the clusterID
        """
        target_device = self.device

        cluster_indices = [i for i, cid in enumerate(self.clusterID) if cid == clusterID]

        if not cluster_indices:
            # No data with the specified clusterID found
            return None

        if return_index:
            choice = torch.tensor(cluster_indices).to(target_device)
            ret_tuple = (choice,)
        else:
            choice = torch.tensor(cluster_indices).to(target_device)
            ret_tuple = ()

        if transform is None:
            transform = lambda x: x

        ret_tuple += (torch.stack([transform(ee.cpu()) for ee in self.examples[cluster_indices]]).to(target_device),)

        for attr_str in self.attributes[1:-1]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str).to(target_device)
                ret_tuple += (attr[cluster_indices],)
        return ret_tuple

    def generate_augment_data(self, mean, std, partition_func):
        """
        Generate augmented data for the memory buffer.
        """
        if not hasattr(self, 'examples'):
            return

        transform30 = transforms.Compose([
            transforms.RandomRotation(30),
            transforms.Normalize(mean, std),
        ])
        transform60 = transforms.Compose([
            transforms.RandomRotation(60),
            transforms.Normalize(mean, std),
        ])
        transform45 = transforms.Compose([
            transforms.RandomRotation(45),
            transforms.Normalize(mean, std),
        ])
        transform75 = transforms.Compose([
            transforms.RandomRotation(75),
            transforms.Normalize(mean, std),
        ])
        
        if hasattr(self, 'examples'):
            with torch.no_grad():
                self.augment_examples = torch.cat([
                    torch.stack([transform30(ee.cpu()) for ee in self.examples]),
                    torch.stack([transform60(ee.cpu()) for ee in self.examples]),
                    torch.stack([transform45(ee.cpu()) for ee in self.examples]),
                    torch.stack([transform75(ee.cpu()) for ee in self.examples]),
                ]).to(self.device)
        
        if hasattr(self, 'labels'):
            with torch.no_grad():
                self.augment_labels = torch.cat([self.labels] * 4).to(self.device)
        
        if hasattr(self, 'logits'):
            self.augment_logits = None
        
        if hasattr(self, 'clusterID'):
            with torch.no_grad():
                self.augment_clusterID = partition_func(self.augment_examples)
        
        if hasattr(self, 'task_labels'):
            with torch.no_grad():
                self.augment_task_labels = torch.cat([self.task_labels] * 4).to(self.device)

    def get_augment_data(self, choice):
        """
        Return augmented data.
        """
        ret_tuple = ()
        if hasattr(self, 'augment_examples'):
            augment_choice = torch.cat([choice,
                                        choice + self.buffer_size,
                                        choice + 2 * self.buffer_size,
                                        choice + 3 * self.buffer_size])
            ret_tuple = (self.augment_examples[augment_choice],)
            for attr_str in ['augment_labels', 'augment_logits', 'augment_clusterID']:
                if hasattr(self, attr_str):
                    attr = getattr(self, attr_str)
                    if attr is not None:
                        ret_tuple += (attr[augment_choice],)
                    else:
                        ret_tuple += (attr,)
            return ret_tuple
        else:
            return None

# Install required package
!pip install py7zr

# -----------------------------
# Required Imports
# -----------------------------

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchvision import datasets, transforms
from torch.utils.data import DataLoader, Dataset
from PIL import Image
import os
import pandas as pd
from tqdm import tqdm
import py7zr
import math

# -----------------------------
# Device Configuration
# -----------------------------

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

# -----------------------------
# Model Definition: ResNet152
# -----------------------------

class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, in_planes, planes, stride=1):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * self.expansion, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes * self.expansion:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes * self.expansion, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * self.expansion)
            )

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = F.relu(self.bn2(self.conv2(out)))
        out = self.bn3(self.conv3(out))
        out += self.shortcut(x)
        out = F.relu(out)
        return out

class ResNet(nn.Module):
    def __init__(self, block, num_blocks, num_classes=10):
        super(ResNet, self).__init__()
        self.in_planes = 64

        # Stem layer
        self.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(64)

        # ResNet layers
        self.layer1 = self._make_layer(block, 64, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, 128, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, 256, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block, 512, num_blocks[3], stride=2)

        # Final classifier layer
        self.linear = nn.Linear(512 * block.expansion, num_classes)

        # Initialize weights
        self._initialize_weights()

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.zeros_(m.bias)

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        out = F.avg_pool2d(out, 4)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out

def ResNet152():
    return ResNet(Bottleneck, [3, 8, 36, 3])

# -----------------------------
# Instantiate the Model
# -----------------------------

model = ResNet152().to(device)
print("Model instantiated successfully.")

# -----------------------------
# Data Augmentation: CutOut
# -----------------------------

class Cutout(object):
    def __init__(self, length):
        self.length = length

    def __call__(self, img):
        h, w = img.size(1), img.size(2)
        mask = torch.ones(h, w, dtype=img.dtype, device=img.device)

        y = torch.randint(h, (1,)).item()
        x = torch.randint(w, (1,)).item()

        y1 = max(0, y - self.length // 2)
        y2 = min(h, y + self.length // 2)
        x1 = max(0, x - self.length // 2)
        x2 = min(w, x + self.length // 2)

        mask[y1:y2, x1:x2] = 0
        img = img * mask.unsqueeze(0)  # Apply mask to all channels
        return img

# -----------------------------
# Data Loading and Preprocessing
# -----------------------------

# CIFAR-10 mean and std for normalization
CIFAR10_MEAN = (0.4914, 0.4822, 0.4465)
CIFAR10_STD = (0.2023, 0.1994, 0.2010)

# Training transformations with CutOut
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize(CIFAR10_MEAN, CIFAR10_STD),
    Cutout(length=16),
])

# Validation/Test transformations
transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize(CIFAR10_MEAN, CIFAR10_STD),
])

# Load CIFAR-10 dataset
trainset = datasets.CIFAR10(root='./data/Train', train=True, download=True, transform=transform_train)
trainloader = DataLoader(trainset, batch_size=128, shuffle=True, num_workers=4)

testset = datasets.CIFAR10(root='./data/Valid', train=False, download=True, transform=transform_test)
testloader = DataLoader(testset, batch_size=100, shuffle=False, num_workers=4)

# -----------------------------
# Define Loss Function and Optimizer
# -----------------------------

criterion = nn.CrossEntropyLoss()

# AdamW optimizer with weight decay
optimizer = optim.AdamW(model.parameters(), lr=0.001, weight_decay=5e-4)

# -----------------------------
# Cosine Annealing Scheduler with Warmup
# -----------------------------

class CosineAnnealingWarmupRestarts(optim.lr_scheduler._LRScheduler):
    def __init__(self, optimizer, T_0, T_mult=1, eta_max=0.001, T_up=5, gamma=0.5, last_epoch=-1):
        self.T_0 = T_0
        self.T_mult = T_mult
        self.base_eta_max = eta_max
        self.eta_max = eta_max
        self.T_up = T_up
        self.gamma = gamma
        self.cycle = 0
        self.T_i = T_0
        super(CosineAnnealingWarmupRestarts, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        if self.last_epoch < self.T_up:
            # Warmup phase
            return [(self.base_lrs[i] + (self.eta_max - self.base_lrs[i]) * self.last_epoch / self.T_up)
                    for i in range(len(self.base_lrs))]
        else:
            # Cosine annealing phase
            cos_inner = (self.last_epoch - self.T_up) / (self.T_i - self.T_up)
            return [self.base_lrs[i] + 0.5 * (self.eta_max - self.base_lrs[i]) *
                    (1 + math.cos(math.pi * cos_inner))
                    for i in range(len(self.base_lrs))]

    def step_ReduceLROnPlateau(self, metrics, epoch=None):
        pass  # Not implemented

scheduler = CosineAnnealingWarmupRestarts(optimizer, T_0=60, T_mult=1, eta_max=0.001, T_up=10, gamma=0.5)

# -----------------------------
# Training and Validation Functions
# -----------------------------

def train(epoch):
    model.train()
    running_loss = 0.0
    for batch_idx, (inputs, targets) in enumerate(tqdm(trainloader, desc=f"Training Epoch {epoch+1}")):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        optimizer.step()

        running_loss += loss.item()
        if batch_idx % 100 == 99:    # Print every 100 mini-batches
            print(f'[Epoch {epoch + 1}, Batch {batch_idx + 1}] loss: {running_loss / 100:.3f}')
            running_loss = 0.0

def validate(epoch):
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for inputs, targets in tqdm(testloader, desc=f"Validation Epoch {epoch+1}"):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = model(inputs)
            _, predicted = torch.max(outputs, 1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()
    accuracy = 100. * correct / total
    print(f'Validation Accuracy after Epoch {epoch + 1}: {accuracy:.2f}%')
    return accuracy

# -----------------------------
# Training Loop with Checkpointing
# -----------------------------

best_acc = 0.0
num_epochs = 70  # Total number of epochs

for epoch in range(num_epochs):
    train(epoch)
    acc = validate(epoch)
    scheduler.step()

    # Save the model checkpoint if it's the best so far
    if acc > best_acc:
        best_acc = acc
        torch.save(model.state_dict(), 'resnet152_cifar10_weights_best.pt')
        print(f"Best model saved with accuracy: {best_acc:.2f}%")

print("Initial Training completed.")

# -----------------------------
# Fine-Tuning Phase
# -----------------------------

# Load the best model from initial training
model.load_state_dict(torch.load('resnet152_cifar10_weights_best.pt'))
model.eval()

# Fine-Tuning Transformations (additional normalization if needed)
transform_finetune = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize(CIFAR10_MEAN, CIFAR10_STD),
    Cutout(length=16),
])

# Update the training dataset with fine-tuning transformations
finetune_trainset = datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_finetune)
finetune_trainloader = DataLoader(finetune_trainset, batch_size=128, shuffle=True, num_workers=4)

# Re-define optimizer for fine-tuning with lower learning rate
finetune_optimizer = optim.AdamW(model.parameters(), lr=0.0001, weight_decay=5e-4)

# Re-define scheduler for fine-tuning
finetune_scheduler = CosineAnnealingWarmupRestarts(finetune_optimizer, T_0=30, T_mult=1, eta_max=0.0001, T_up=5, gamma=0.5)

def finetune(epoch):
    model.train()
    running_loss = 0.0
    for batch_idx, (inputs, targets) in enumerate(tqdm(finetune_trainloader, desc=f"Fine-Tuning Epoch {epoch+1}")):
        inputs, targets = inputs.to(device), targets.to(device)
        finetune_optimizer.zero_grad()
        outputs = model(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        finetune_optimizer.step()

        running_loss += loss.item()
        if batch_idx % 100 == 99:
            print(f'[Fine-Tuning Epoch {epoch + 1}, Batch {batch_idx + 1}] loss: {running_loss / 100:.3f}')
            running_loss = 0.0

def finetune_validate(epoch):
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for inputs, targets in tqdm(testloader, desc=f"Fine-Tuning Validation Epoch {epoch+1}"):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = model(inputs)
            _, predicted = torch.max(outputs, 1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()
    accuracy = 100. * correct / total
    print(f'Fine-Tuning Validation Accuracy after Epoch {epoch + 1}: {accuracy:.2f}%')
    return accuracy

# Fine-Tuning loop
fine_tune_epochs = 15
for epoch in range(fine_tune_epochs):
    finetune(epoch)
    acc = finetune_validate(epoch)
    finetune_scheduler.step()

    # Save the fine-tuned model checkpoint if it's the best so far
    if acc > best_acc:
        best_acc = acc
        torch.save(model.state_dict(), 'resnet152_cifar10_finetuned_best.pt')
        print(f"Fine-Tuned best model saved with accuracy: {best_acc:.2f}%")

print("Fine-Tuning completed.")

---
title: "GPU 集群运维（10）：自动化与平台工程"
date: 2026-11-16T09:00:00+08:00
draft: false
description: "自动化体系总览、Ansible 实战、批量执行工具、装机自动化、自愈系统、GitOps、巡检平台化、平台门户功能清单与工具箱沉淀方法。"
tags: ["自动化", "GitOps", "Ansible"]
series: "GPU 集群运维"
---

## 本册环境基线

> 以下版本是本册所有命令、YAML、脚本的参考基准，与 `00-版本基线.md` 保持一致。

| 组件 | 基线版本 | 本册重点用法 |
|---|---|---|
| K8s | 1.31.x | DaemonSet/Deployment/Service/AlertmanagerConfig |
| Ansible | ansible-core 2.17.x（ansible 10 套件） | playbook/role inventory/驱动安装 role |
| pdsh / clustershell | pdsh 2.34 / clustershell 1.9 | 批量 SSH 巡检 |
| PXE（Ubuntu 路径） | Ubuntu 22.04 autoinstall（cloud-init 24.x，#cloud-config v1） | PXE 装机 |
| PXE（Rocky 路径） | Rocky 9.5 kickstart（RHEL 9 语法） | PXE 装机 |
| ArgoCD | v2.13.x（argoproj.io/v1alpha1） | ApplicationSet/GitOps |
| JupyterHub | chart 3.3.x（示例，以官方 release 为准）+ KubeSpawner | 开发环境 |
| KServe | v0.14.x（serving.kserve.io/v1beta1，示例） | InferenceService 推理服务 |
| 驱动（Hopper/Blackwell） | R570 生产分支，570.124.04（示例） | 驱动安装 role/批量升级 |
| 驱动（Ampere 存量） | R550 生产分支，550.127.08（示例） | 同上 |
| DCGM | 3.3.9 / 4.2.x | dcgmi diag 巡检 |
| OS | Ubuntu 22.04.5 LTS / Rocky 9.5 | 双基线 |
| Python（系统） | 3.10（Ubuntu 22.04）/ 3.9（Rocky 9.5） | 自愈 webhook 脚本 |
| kubernetes Python client | 28.x（示例，以官方 release 为准） | 自愈 webhook 脚本 |

---

# 10 自动化与平台工程

## 1 自动化体系总览

### 1.1 成熟度阶梯

```text
Level 0  脚本散落   → 各节点 /home/admin 下散落 sh 脚本，无版本控制
Level 1  SOP 固化   → 脚本入 Git，编写 runbook，人工按步骤执行
Level 2  半自动     → Ansible playbook 批量执行，有 dry-run 和回滚
Level 3  自愈闭环   → 告警→诊断→处置全链路自动，人工只介入升级场景
Level 4  平台化     → 自助门户 + API 驱动，运维能力产品化
```

| 成熟度 | 典型特征 | MTTR（Mean Time To Repair） | 人工介入比例 |
|--------|---------|--------------------------|------------|
| Level 0 | SSH 逐台登录、手动操作 | 2~4 小时 | 100% |
| Level 1 | Runbook 指导操作 | 30~60 分钟 | 90% |
| Level 2 | Ansible 批量执行 | 10~30 分钟 | 50% |
| Level 3 | Webhook 触发自动处置 | 3~10 分钟 | 15% |
| Level 4 | 全自助 + 自动扩缩 | < 3 分钟 | 5% |

---

## 2 Ansible 实战

### 2.1 Inventory 组织

```ini
# inventory/gpu-cluster.ini
# 适用: ansible-core 2.17.x（ansible 10 套件）/ INI 格式 inventory
# 注意：ansible.builtin 集合内置模块全限定名在 2.17 中为推荐写法（如 ansible.builtin.apt）
[gpu_h100]
gpu-h100-[001:064] ansible_user=root

[gpu_a100]
gpu-a100-[001:032] ansible_user=root

[gpu_inference:children]
gpu_a100
gpu_h100
# 推理节点组，包含所有 GPU 节点

[management]
mgmt-[001:003] ansible_user=ops

[storage]
nas-[001:002] ansible_user=root
minio-[001:003] ansible_user=root

[gpu_cluster:children]
gpu_h100
gpu_a100
management

[gpu_cluster:vars]
ansible_ssh_common_args='-o StrictHostKeyChecking=no -o ProxyJump=bastion'
ansible_python_interpreter=/usr/bin/python3
```

```yaml
# inventory/group_vars/gpu_h100.yaml
# 适用: ansible-core 2.17.x / YAML 格式 group_vars
# 版本钉死：driver_version 与 00-基线对齐（H100 主基线 R570，550.54.15 为 R550 示例适用于 Ampere 存量；H100 应改为 570.124.04）
---
gpu_model: "H100_SXM"
gpu_count: 8
driver_version: "570.124.04"   # 基线：H100 主基线 R570（原稿 550.54.15 为 Ampere 存量基线，此处修正为 H100 正确版本）
cuda_version: "12.8"           # 基线：H100 主基线 CUDA 12.8（原稿 12.4 为 Ampere 兼容基线，此处修正）
container_runtime: "containerd"
containerd_version: "1.7.13"
nvidia_container_toolkit_version: "1.17.4"  # 基线：nvidia-container-toolkit v1.17.4（原稿 1.15.0 已过时）
power_limit_watts: 700
persistence_mode: true
ib_device: "mlx5_0"
```

### 2.2 驱动安装 Role 完整示例

```yaml
# roles/nvidia-driver/tasks/main.yml
# 适用: ansible-core 2.17.x / 双 OS 路径：
#   - apt 路径: Ubuntu 22.04.5 LTS（apt 模块 = ansible.builtin.apt）
#   - yum/dnf 路径: Rocky 9.5（yum 模块 = ansible.builtin.yum；RHEL 9 推荐 dnf，yum 为兼容别名）
# 驱动安装方式：runfile（通用，适用于两 OS）；apt 包方式见注释
# 驱动版本：R570/570.124.04（H100 主基线）或 R550/550.127.08（Ampere 存量基线）
---
# ========== 阶段 1：禁用 nouveau ==========
- name: 检查 nouveau 是否已加载
  command: lsmod
  register: lsmod_output
  changed_when: false

- name: 创建 nouveau 黑名单文件
  copy:
    dest: /etc/modprobe.d/blacklist-nouveau.conf
    content: |
      blacklist nouveau
      options nouveau modeset=0
    mode: "0644"
  when: "'nouveau' in lsmod_output.stdout"
  register: nouveau_blacklisted

- name: 重建 initramfs（Debian/Ubuntu）
  command: update-initramfs -u
  when:
    - nouveau_blacklisted is changed
    - ansible_os_family == "Debian"

- name: 重建 initramfs（RHEL/CentOS）
  command: dracut --force
  when:
    - nouveau_blacklisted is changed
    - ansible_os_family == "RedHat"

# ========== 阶段 2：安装系统依赖 ==========
- name: 安装基础依赖（Debian/Ubuntu）
  apt:
    name:
      - build-essential
      - linux-headers-{{ ansible_kernel }}
      - pkg-config
      - libglvnd-dev
      - dkms
    state: present
    update_cache: yes
  when: ansible_os_family == "Debian"

- name: 安装基础依赖（RHEL/CentOS）
  yum:
    name:
      - gcc
      - kernel-devel-{{ ansible_kernel }}
      - kernel-headers-{{ ansible_kernel }}
      - pkgconfig
      - libglvnd-devel
      - dkms
    state: present
  when: ansible_os_family == "RedHat"

# ========== 阶段 3：下载并安装驱动（runfile 方式）==========
- name: 检查当前驱动版本
  command: nvidia-smi --query-gpu=driver_version --format=csv,noheader
  register: current_driver
  changed_when: false
  failed_when: false

- name: 设置驱动安装状态变量
  set_fact:
    driver_needs_install: >-
      {{ current_driver.rc != 0 or
         (current_driver.stdout | trim) != driver_version }}

- name: 下载 NVIDIA 驱动 runfile
  get_url:
    url: "https://us.download.nvidia.com/tesla/{{ driver_version }}/NVIDIA-Linux-x86_64-{{ driver_version }}.run"
    dest: "/tmp/NVIDIA-Linux-x86_64-{{ driver_version }}.run"
    mode: "0755"
    checksum: "sha256:{{ driver_sha256 }}"
  when: driver_needs_install

- name: 停止可能占用 GPU 的服务
  systemd:
    name: "{{ item }}"
    state: stopped
  loop:
    - nvidia-persistenced
    - nvidia-fabricmanager
    - kubelet
  failed_when: false
  when: driver_needs_install

- name: 卸载旧驱动（若存在）
  command: /usr/bin/nvidia-uninstall --silent
  failed_when: false
  when:
    - driver_needs_install
    - current_driver.rc == 0

- name: 安装 NVIDIA 驱动（runfile）
  command: >
    /tmp/NVIDIA-Linux-x86_64-{{ driver_version }}.run
    --silent
    --dkms
    --no-questions
    --disable-nouveau
    --install-libglvnd
  register: driver_install
  when: driver_needs_install

- name: 清理 runfile 安装包
  file:
    path: "/tmp/NVIDIA-Linux-x86_64-{{ driver_version }}.run"
    state: absent

# ========== 阶段 4：验证安装 ==========
- name: 验证驱动加载
  command: nvidia-smi
  register: nvidia_smi_result
  retries: 3
  delay: 5
  until: nvidia_smi_result.rc == 0
  when: driver_needs_install

- name: 验证 GPU 数量
  command: nvidia-smi --query-gpu=count --format=csv,noheader
  register: gpu_count_result
  changed_when: false

- name: 断言 GPU 数量正确
  assert:
    that:
      - (gpu_count_result.stdout | trim | int) == gpu_count
    fail_msg: "期望 {{ gpu_count }} 张 GPU，实际 {{ gpu_count_result.stdout | trim }}"

- name: 显示驱动信息
  debug:
    msg: "NVIDIA 驱动 {{ driver_version }} 安装成功，GPU 数量: {{ gpu_count_result.stdout | trim }}"

# ========== 阶段 5：配置 persistenced ==========
- name: 启用 nvidia-persistenced（避免 GPU 掉卡）
  systemd:
    name: nvidia-persistenced
    state: started
    enabled: yes

- name: 配置 GPU 功率封顶
  command: "nvidia-smi -pl {{ power_limit_watts }}"
  when: power_limit_watts is defined and power_limit_watts != 700

- name: 配置 GPU Persistence Mode 开机自启
  copy:
    dest: /etc/systemd/system/nvidia-power-cap.service
    content: |
      [Unit]
      Description=NVIDIA GPU Power Cap
      After=nvidia-persistenced.service
      [Service]
      Type=oneshot
      ExecStart=/usr/bin/nvidia-smi -pl {{ power_limit_watts }}
      RemainAfterExit=yes
      [Install]
      WantedBy=multi-user.target
    mode: "0644"
  when: power_limit_watts is defined and power_limit_watts != 700
  notify: reload systemd

- name: 启用 GPU 功率封顶服务
  systemd:
    name: nvidia-power-cap
    state: started
    enabled: yes
    daemon_reload: yes
  when: power_limit_watts is defined and power_limit_watts != 700

# ========== 阶段 6：恢复服务 ==========
- name: 恢复 kubelet
  systemd:
    name: kubelet
    state: started
  when: driver_needs_install

- name: 恢复 nvidia-fabricmanager（如存在）
  systemd:
    name: nvidia-fabricmanager
    state: started
  failed_when: false
```

```yaml
# roles/nvidia-driver/handlers/main.yml
# 适用: ansible-core 2.17.x（reboot 模块 = ansible.builtin.reboot，2.17 中全限定名为推荐写法）
---
- name: reload systemd
  systemd:
    daemon_reload: yes

- name: reboot node
  reboot:
    msg: "NVIDIA driver installation requires reboot"
    reboot_timeout: 600
    post_reboot_delay: 60
    test_command: nvidia-smi
```

```yaml
# roles/nvidia-driver/vars/main.yml
# 适用: ansible-core 2.17.x / 驱动版本与 00-基线对齐
---
driver_version: "570.124.04"   # H100 主基线 R570（Ampere 存量改 550.127.08）
driver_sha256: "abc123def456..."  # 发布时从 NVIDIA 官网获取并填入（R570 与 R550 校验和不同，必须对应）
gpu_count: 8
power_limit_watts: 700
```

### 2.3 批量巡检 Playbook 完整示例

```yaml
# playbooks/gpu-inspection.yaml
# 适用: ansible-core 2.17.x / 依赖: nvidia-smi（driver R550+）、dcgmi（DCGM 3.3.9/4.2.x）、ibstat（OFED 24.10）
---
- name: GPU 集群批量巡检
  hosts: gpu_cluster
  gather_facts: yes
  become: yes
  vars:
    inspection_report_dir: "/tmp/gpu-inspection-{{ ansible_date_time.date }}"
    min_driver_version: "550.0"  # 最低驱动版本（R550 基线）；H100 节点建议改为 "570.0"
    expected_gpu_temp_max: 85
    expected_ecc_errors_max: 0

  tasks:
    - name: 创建巡检报告目录
      file:
        path: "{{ inspection_report_dir }}"
        state: directory
        mode: "0755"

    # ========== 驱动与 GPU 状态 ==========
    - name: 获取 GPU 状态信息
      command: >
        nvidia-smi --query-gpu=index,name,driver_version,temperature.gpu,
        power.draw,power.limit,memory.used,memory.total,pstate,ecc.errors.uncorrected.volatile.total
        --format=csv,noheader
      register: gpu_status
      changed_when: false

    - name: 获取 DCGM 诊断结果
      command: dcgmi diag -r 1
      register: dcgm_diag
      changed_when: false
      failed_when: false

    - name: 检查 PCIe 带宽
      command: nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.width.current --format=csv,noheader
      register: pcie_status
      changed_when: false

    - name: 检查 ECC 错误
      command: >
        nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total,ecc.errors.corrected.volatile.total
        --format=csv,noheader
      register: ecc_errors
      changed_when: false

    # ========== 系统状态 ==========
    - name: 检查内核日志中的 GPU 错误
      shell: |
        dmesg -T | grep -iE 'xid|nvrm|gpu.*error|nvidia.*fault' | tail -20
      register: dmesg_gpu_errors
      changed_when: false
      failed_when: false

    - name: 检查 IB 网络状态
      command: ibstat {{ ib_device }}
      register: ib_status
      changed_when: false
      failed_when: false

    - name: 检查磁盘空间（/var/lib/kubelet）
      command: df -h /var/lib/kubelet
      register: disk_status
      changed_when: false

    # ========== 汇总报告 ==========
    - name: 生成巡检报告
      template:
        src: templates/inspection-report.j2
        dest: "{{ inspection_report_dir }}/{{ inventory_hostname }}.txt"
        mode: "0644"

    - name: 汇总所有节点报告
      run_once: yes
      delegate_to: localhost
      become: no
      shell: |
        cat {{ hostvars[item]['inspection_report_dir'] }}/*.txt > /tmp/gpu-inspection-summary-{{ ansible_date_time.date }}.txt
      with_items: "{{ groups['gpu_cluster'] }}"

    - name: 输出异常节点列表
      run_once: yes
      debug:
        msg: "巡检完成。详细报告: /tmp/gpu-inspection-summary-{{ ansible_date_time.date }}.txt"
```

### 2.4 批量升级驱动 Playbook 骨架

```yaml
# playbooks/gpu-driver-upgrade.yaml
# 适用: ansible-core 2.17.x / K8s 1.31（kubectl cordon/drain/uncordon）
# 目标驱动版本：R570/570.124.04（H100 主基线）；批量升级走 serial 控制爆炸半径
---
- name: GPU 驱动批量升级
  hosts: gpu_cluster
  serial: "{{ batch_size | default(4) }}"    # 每批 4 台，逐步扩大
  become: yes
  vars:
    target_driver_version: "570.124.04"  # 基线 R570（原稿 560.35.03 非生产分支，已修正为基线版本）
    drain_timeout: 600
    verify_timeout: 300

  pre_tasks:
    - name: 确认目标版本与当前版本不同
      command: nvidia-smi --query-gpu=driver_version --format=csv,noheader
      register: current_version
      changed_when: false

    - name: 跳过已是目标版本的节点
      meta: end_host
      when: (current_version.stdout | trim) == target_driver_version

    - name: Cordon 节点（禁止新 pod 调度）
      delegate_to: "{{ k8s_master }}"
      command: "kubectl cordon {{ inventory_hostname }}"

    - name: Drain 节点（驱逐现有 pod）
      delegate_to: "{{ k8s_master }}"
      command: >
        kubectl drain {{ inventory_hostname }}
        --ignore-daemonsets
        --delete-emptydir-data
        --force
        --timeout={{ drain_timeout }}s

  roles:
    - role: nvidia-driver
      vars:
        driver_version: "{{ target_driver_version }}"

  post_tasks:
    - name: 验证驱动版本
      command: nvidia-smi --query-gpu=driver_version --format=csv,noheader
      register: new_version
      until: (new_version.stdout | trim) == target_driver_version
      retries: 6
      delay: 10

    - name: 运行 DCGM 快速诊断
      command: dcgmi diag -r 1
      register: dcgm_result
      failed_when: dcgm_result.rc != 0

    - name: Uncordon 节点（恢复调度）
      delegate_to: "{{ k8s_master }}"
      command: "kubectl uncordon {{ inventory_hostname }}"

    - name: 等待节点 Ready
      delegate_to: "{{ k8s_master }}"
      command: >
        kubectl wait --for=condition=Ready node/{{ inventory_hostname }}
        --timeout={{ verify_timeout }}s

    - name: 等待 GPU 资源可调度
      delegate_to: "{{ k8s_master }}"
      shell: |
        kubectl get node {{ inventory_hostname }} \
          -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
      register: gpu_allocatable
      until: (gpu_allocatable.stdout | int) == gpu_count
      retries: 12
      delay: 10

  handlers:
    - name: reboot node
      reboot:
        msg: "Driver upgrade requires reboot"
        reboot_timeout: 600
        post_reboot_delay: 120
        test_command: nvidia-smi
```

---

## 3 批量执行工具

### 3.1 pdsh / clush 用法示例

```bash
# pdsh（Parallel Distributed Shell）- 并行 SSH 执行
# 安装：apt install pdsh（Ubuntu 22.04，pdsh 2.34）或 yum install pdsh（Rocky 9.5，pdsh 2.34）
# clustershell: pip install clustershell==1.9 或 apt install clustershell（Ubuntu 22.04）/ dnf install clustershell（Rocky 9.5）

# 在所有 GPU 节点执行 nvidia-smi
pdsh -w gpu-h100-[001-064] "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader"

# 使用 dshbak 整理输出（按节点分组显示）
pdsh -w gpu-h100-[001-064] "nvidia-smi" | dshbak -c

# 限制并发数（-f 参数）
pdsh -f 16 -w gpu-h100-[001-064] "nvidia-smi --query-gpu=power.draw --format=csv"

# -----------------
# clush（clustershell）- 功能更强大
# 安装：pip install clustershell 或 apt install clustershell

# 基础并行执行
clush -w gpu-h100-[001-064] "nvidia-smi -L"

# 带超时和失败容忍（-u 超时秒数，-f 并发数）
clush -w gpu-h100-[001-064] -f 32 -u 30 "dcgmi diag -r 1"

# 分组输出（相同输出的节点合并显示）
clush -w gpu-h100-[001-064] -b "nvidia-smi --query-gpu=driver_version --format=csv,noheader"

# 文件分发（批量复制配置文件）
clush -w gpu-h100-[001-064] --copy /etc/nvidia/gridd.conf --dest /etc/nvidia/

# 从节点收集文件
clush -w gpu-h100-[001-064] --rcopy /var/log/nvidia-installer.log --dest /tmp/collected/
```

### 3.2 并行 SSH 巡检一行流

```bash
# 一行流：检查所有 GPU 节点的温度、功率、ECC 错误，带超时和失败容忍
clush -w gpu-h100-[001-064] -f 32 -u 30 --diff \
  "nvidia-smi --query-gpu=index,temperature.gpu,power.draw,ecc.errors.uncorrected.volatile.total \
   --format=csv,noheader" \
  2>&1 | tee /tmp/gpu-health-$(date +%Y%m%d-%H%M).log

# 带失败统计的完整巡检一行流
(echo "=== GPU Cluster Health Check $(date) ===" && \
 clush -w gpu-h100-[001-064] -f 32 -u 30 \
   "hostname && \
    nvidia-smi --query-gpu=index,temperature.gpu,power.draw,memory.used,pstate \
    --format=csv,noheader && \
    dmesg -T 2>/dev/null | grep -ic 'xid' || echo 0" \
 2>&1; \
 echo "=== Failed nodes ===" && \
 clush -w gpu-h100-[001-064] -f 32 -u 10 -e "ssh -o ConnectTimeout=5" \
   "echo OK" 2>&1 | grep -v OK) \
 | tee /tmp/gpu-full-inspection-$(date +%Y%m%d-%H%M).log
```

---

## 4 装机自动化

### 4.1 PXE 装机流程要点

```text
PXE 启动流程（双路径：Ubuntu 22.04.5 LTS autoinstall + Rocky 9.5 kickstart；以下为通用流程，OS 差异在 bootloader 参数与安装配置文件）：
  1. 服务器 BIOS/UEFI 设置网络启动为第一启动项
  2. PXE Client 发送 DHCP Discover（带 Option 60 = PXEClient）
  3. DHCP Server 回复 IP + next-server（TFTP 地址）+ filename（bootloader 路径）
  4. PXE Client 从 TFTP 下载 bootloader（grub2/syslinux/pxelinux）
  5. Bootloader 加载内核 + initrd（含 kickstart/autoinstall 参数）
  6. 内核启动 → 执行 kickstart/autoinstall 自动安装

DHCP 配置片段（ISC DHCP）：
  subnet 10.10.0.0 netmask 255.255.0.0 {
    range 10.10.1.100 10.10.1.200;
    option routers 10.10.0.1;
    option domain-name-servers 10.10.0.2;
    next-server 10.10.0.10;          # TFTP 服务器
    filename "grub/grubx64.efi";    # UEFI bootloader
  }

TFTP 目录结构（双 OS 路径，Ubuntu 22.04 autoinstall + Rocky 9.5 kickstart）：
  /tftpboot/
  ├── grub/
  │   └── grubx64.efi
  ├── ubuntu-22.04/          # Ubuntu 22.04.5 LTS autoinstall 路径（内核 vmlinuz + initrd 来自 ubuntu-22.04.5-live-server-amd64.iso）
  │   ├── vmlinuz
  │   └── initrd
  ├── rocky-9.5/             # Rocky 9.5 kickstart 路径（内核 vmlinuz + initrd 来自 Rocky-9.5-x86_64-dvd.iso）
  │   ├── vmlinuz
  │   └── initrd
  └── ks/
      └── gpu-node.cfg              # kickstart/autoinstall 配置（Rocky 9 用 .ks，Ubuntu 22.04 用 user-data + meta-data）
```

**Kickstart 示例关键段（GPU 节点特化）**：

```bash
# gpu-node.ks - GPU 计算节点 kickstart 配置
# 适用: Rocky Linux 9.5 / RHEL 9 kickstart 语法（与 RHEL 8 差异：%packages 中 @server 改为 @^server-product-environment，repo 配置使用 url= 而非 baseurl=）
# Ubuntu 22.04 对应方案：autoinstall + user-data（cloud-init 24.x 格式，见 4.2 节）；此处为 Rocky 9 kickstart 路径

install
url --url=http://10.10.0.10/mirror/ubuntu-22.04/
lang en_US.UTF-8
keyboard us
timezone Asia/Shanghai --utc

# 网络配置（管理网口）
network --bootproto=dhcp --device=enp1s0f0 --hostname=gpu-node
network --bootproto=static --device=enp1s0f1 --ip=10.20.0.X --netmask=255.255.0.0 --noipv6 --onboot=yes

# 分区方案（本地 NVMe + GPU 节点特化）
clearpart --all --initlabel
part /boot/efi --fstype=vfat --size=512
part /boot --fstype=ext4 --size=2048

# 系统盘 NVMe（/dev/nvme0n1）
part pv.system --fstype=lvmpv --ondisk=nvme0n1 --size=1 --grow
volgroup vg_system pv.system
logvol /        --vgname=vg_system --fstype=ext4 --size=51200 --name=lv_root
logvol /var     --vgname=vg_system --fstype=ext4 --size=102400 --name=lv_var
logvol /var/log --vgname=vg_system --fstype=ext4 --size=51200 --name=lv_varlog
logvol /tmp     --vgname=vg_system --fstype=ext4 --size=20480 --name=lv_tmp
logvol swap     --vgname=vg_system --fstype=swap --size=65536 --name=lv_swap

# 数据盘 NVMe（/dev/nvme1n1，用于模型数据和 checkpoint）
part pv.data --fstype=lvmpv --ondisk=nvme1n1 --size=1 --grow
volgroup vg_data pv.data
logvol /data    --vgname=vg_data --fstype=xfs --size=1 --grow --name=lv_data

# 内核参数（GPU 优化）
bootloader --append="iommu=pt intel_iommu=on hugepagesz=1G hugepages=64 transparent_hugepage=never"

# 基础软件包
%packages
@server
openssh-server
chrony
curl
wget
jq
iperf3
lm-sensors
ipmitool
numactl
hwloc
pciutils
%end

# 安装后脚本
%post --interpreter=/bin/bash
set -ex

# 禁用不必要的服务
systemctl disable bluetooth cups ModemManager

# 配置 chrony（内网 NTP）
cat > /etc/chrony/chrony.conf <<'CHRONY'
server 10.10.0.1 iburst
driftfile /var/lib/chrony/drift
makestep 1.0 3
rtcsync
CHRONY
systemctl enable chronyd

# 配置内核参数
cat >> /etc/sysctl.conf <<'SYSCTL'
# GPU 集群优化
vm.max_map_count=262144
fs.file-max=2097152
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=65535
kernel.numa_balancing=0
SYSCTL

# 配置 SSH 安全
sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
echo "AllowUsers root ops deploy" >> /etc/ssh/sshd_config

# 注册到配置管理（Ansible/Puppet/Salt）
curl -s http://10.10.0.10/register.sh | bash -s -- "$(hostname)"

%end

reboot
```

### 4.2 Cloud-init 首启配置完整示例

```yaml
# cloud-init/user-data.yaml
# 适用: Ubuntu 22.04.5 LTS / cloud-init 24.x（#cloud-config 格式 v1，schema v1 自 cloud-init 22.1 起为默认）
# Rocky 9 路径：cloud-init 23.x（el9 源），语法相同，但 apt-get 命令替换为 dnf install
#cloud-config
hostname: gpu-node-${INSTANCE_ID}
manage_etc_hosts: true

users:
  - name: ops
    groups: sudo, docker
    shell: /bin/bash
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOpskey... ops@mgmt-001
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDeploykey... deploy@ci-server

ssh_pwauth: false

chrony:
  ntpsources:
    - hostname: 10.10.0.1
      minpoll: 4
      maxpoll: 8

write_files:
  # 内核参数
  - path: /etc/sysctl.d/99-gpu-cluster.conf
    content: |
      vm.max_map_count=262144
      fs.file-max=2097152
      net.core.somaxconn=65535
      net.core.netdev_max_backlog=65535
      net.ipv4.tcp_max_syn_backlog=65535
      net.ipv4.tcp_keepalive_time=600
      net.ipv4.tcp_keepalive_intvl=30
      net.ipv4.tcp_keepalive_probes=5
      kernel.numa_balancing=0
      # 大页内存（HPC / 大模型训练优化）
      vm.nr_hugepages=64

  # 禁用透明大页（GPU 训练推荐）
  - path: /etc/systemd/system/disable-thp.service
    content: |
      [Unit]
      Description=Disable Transparent Huge Pages
      DefaultDependencies=no
      After=sysinit.target local-fs.target
      Before=basic.target
      [Service]
      Type=oneshot
      ExecStart=/bin/sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'
      ExecStart=/bin/sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/defrag'
      [Install]
      WantedBy=basic.target

  # 拉取配置管理脚本
  - path: /opt/bootstrap/register.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      NODE_NAME=$(hostname)
      MGMT_SERVER="10.10.0.10"

      # 向管理节点注册
      curl -sf "http://${MGMT_SERVER}:8080/api/v1/nodes/register" \
        -H "Content-Type: application/json" \
        -d "{\"hostname\": \"${NODE_NAME}\", \"ip\": \"$(hostname -I | awk '{print $1}')\"}"

      # 拉取 Ansible 配置
      mkdir -p /etc/ansible
      curl -sf "http://${MGMT_SERVER}:8080/config/ansible.cfg" -o /etc/ansible/ansible.cfg

      # 标记初始化完成
      touch /var/lib/node-initialized
      echo "$(date) - Node ${NODE_NAME} initialized" >> /var/log/bootstrap.log

runcmd:
  # 应用内核参数
  - sysctl --system

  # 启用禁用 THP 服务
  - systemctl daemon-reload
  - systemctl enable --now disable-thp.service

  # 安装基础工具
  - apt-get update
  - apt-get install -y curl wget jq ipmitool lm-sensors numactl hwloc pciutils

  # 执行注册脚本
  - /opt/bootstrap/register.sh

  # 通知管理节点装机完成
  - 'curl -sf "http://10.10.0.10:8080/api/v1/nodes/$(hostname)/ready" -X POST'

final_message: "GPU node initialized after $UPTIME seconds"
```

### 4.3 装机后自动验收流水线设计

```text
流水线步骤：
  Step 1 [装机]  → PXE/Kickstart 自动安装 OS
  Step 2 [初始化] → cloud-init 执行首启配置
  Step 3 [验收]  → 自动运行验收脚本（见下）
  Step 4 [判定]  → 达标 → 加入 K8s 集群可用节点池
                  → 不达标 → 告警 + 隔离到 quarantine 节点组

验收脚本核心检查项（/opt/bootstrap/acceptance-test.sh）：
  ✓ CPU 核数 = 预期值
  ✓ 内存总量 ≥ 预期值的 95%
  ✓ GPU 数量 = 预期值 && nvidia-smi 正常
  ✓ 所有 NVMe 盘在线且 SMART 健康
  ✓ IB/RDMA 网络连通性（ping + ib_send_bw 微基准）
  ✓ 管理网络 SSH 连通
  ✓ chrony 时间同步偏差 < 10ms
  ✓ 内核版本 = 预期版本
  ✓ 驱动版本 = 预期版本
```

```yaml
# acceptance-test Job（K8s 外，裸金属节点跑 systemd service）
# /etc/systemd/system/gpu-acceptance-test.service
#
# [Unit]
# Description=GPU Node Acceptance Test
# After=network-online.target nvidia-persistenced.service
# Wants=network-online.target
#
# [Service]
# Type=oneshot
# ExecStart=/opt/bootstrap/acceptance-test.sh
# RemainAfterExit=yes
#
# [Install]
# WantedBy=multi-user.target
```

---

## 5 自愈系统

### 5.1 架构总览

![自愈系统链路：告警源经 Webhook 网关到诊断引擎与处置动作库，工单闭环](/images/gpu-10-selfheal.svg)

### 5.2 处置动作库设计表

| 触发条件 | 诊断步骤 | 自动动作 | 人工升级条件 |
|---------|---------|---------|------------|
| XID 79（GPU 掉卡） | 1. 检查 dmesg XID 记录 2. 检查 GPU 温度 3. 检查 PCIe 状态 | 1. Drain 节点 2. Reset GPU (`nvidia-smi -r -i <id>`) 3. 若恢复则 Uncordon；若未恢复则隔离节点 | Reset 后仍报 XID 79 → 创建硬件工单 |
| XID 48（双位 ECC） | 1. 查 ECC 错误计数 2. 确认受影响 GPU | 1. Drain 节点 2. 标记节点 `gpu-error=ecc` | ECC 累计 > 10 → 更换 GPU |
| 温度 > 95°C 持续 2m | 1. 检查风扇转速 2. 检查液冷流量/温度 3. 检查环境温度 | 1. 降低功率封顶至 400W 2. 通知运维 | 降温后仍 > 90°C → 下电检查散热 |
| 节点 NotReady > 5m | 1. Ping 检测网络 2. 检查 kubelet 日志 3. 检查容器运行时 | 1. IPMI 重启 kubelet 2. 若无效则 IPMI 重启整机 | 重启 2 次仍 NotReady → 硬件检查 |
| RDMA 链路中断 | 1. ibstat 检查端口状态 2. 检查交换机端口 | 1. 隔离节点（cordon） 2. 迁移受影响 Pod | ibstat 反复 flapping → 检查光模块/线缆 |
| 磁盘满（/var > 95%） | 1. du 查找大文件 2. 检查容器日志/镜像 | 1. 清理过期镜像 `crictl rmi --prune` 2. 清理旧日志 | 清理后仍 > 90% → 扩容或迁移 |

### 5.3 XID 79 自愈 Playbook 伪代码

```python
#!/usr/bin/env python3
"""
XID 79 自愈决策逻辑
触发：Prometheus alertmanager webhook → /webhook/xid79
适用: Python 3.10（Ubuntu 22.04 系统版本）/ kubernetes client 28.x（示例，以官方 release 为准）
依赖: pip install requests==2.31.0 kubernetes==28.1.0（版本为示例，实际按 requirements.txt 钉死）
"""

import subprocess
import time
import logging
import requests

logger = logging.getLogger("self-heal-xid79")

def handle_xid79(alert):
    """
    XID 79 = GPU has fallen off the bus（GPU 掉卡/总线故障）
    严重程度：高（可能是硬件故障，也可能是瞬态干扰）
    """
    node_name = alert["labels"]["node"]
    gpu_id = alert["labels"]["gpu"]
    xid_value = alert["labels"]["xid"]

    logger.info(f"XID {xid_value} detected on {node_name} GPU {gpu_id}")

    # Step 1: Drain 节点（防止新作业调度到故障节点）
    logger.info(f"Step 1: Cordoning node {node_name}")
    run_cmd(f"kubectl cordon {node_name}")
    run_cmd(
        f"kubectl drain {node_name} "
        f"--ignore-daemonsets --delete-emptydir-data --force --timeout=600s"
    )

    # Step 2: 等待 30 秒让 I/O 刷盘
    time.sleep(30)

    # Step 3: 尝试 GPU Reset
    logger.info(f"Step 3: Attempting GPU reset on {node_name}:{gpu_id}")
    reset_result = run_cmd_on_node(
        node_name,
        f"nvidia-smi -r -i {gpu_id}"
    )

    if "GPU reset succeeded" in reset_result:
        logger.info("GPU reset succeeded, verifying...")
        time.sleep(15)

        # Step 4: 验证 GPU 恢复
        verify_result = run_cmd_on_node(
            node_name,
            f"nvidia-smi -i {gpu_id} --query-gpu=name,temperature.gpu,pstate --format=csv,noheader"
        )

        if verify_result and "Error" not in verify_result:
            logger.info(f"GPU verified OK: {verify_result}")

            # Step 5: 运行 DCGM 快速诊断确认
            dcgm_result = run_cmd_on_node(node_name, "dcgmi diag -r 1")

            if dcgm_result and "FAIL" not in dcgm_result:
                # Step 6: 恢复节点
                logger.info(f"Step 6: Uncordoning node {node_name}")
                run_cmd(f"kubectl uncordon {node_name}")
                notify_success(node_name, gpu_id)
                return "RESOLVED"

    # 所有自动恢复步骤失败 → 升级人工处理
    logger.error(f"Auto-heal failed for {node_name}:{gpu_id}, escalating")
    run_cmd(f"kubectl label node {node_name} gpu-platform/quarantine=true --overwrite")
    escalate_to_human(node_name, gpu_id, xid_value, reset_result)
    return "ESCALATED"


def run_cmd(cmd):
    """在管理节点执行命令"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return result.stdout.strip()


def run_cmd_on_node(node, cmd):
    """通过 SSH 在目标节点执行命令"""
    result = subprocess.run(
        f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{node} '{cmd}'",
        shell=True, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        logger.warning(f"Command failed on {node}: {result.stderr}")
        return None
    return result.stdout.strip()


def notify_success(node, gpu_id):
    """通知自愈成功"""
    requests.post(
        "https://hooks.slack.com/services/xxx/yyy/zzz",
        json={"text": f"[自愈成功] {node} GPU {gpu_id} XID 79 已自动恢复"}
    )


def escalate_to_human(node, gpu_id, xid, error_detail):
    """升级人工处理：创建工单 + 通知"""
    requests.post(
        "https://jira.internal/rest/api/2/issue",
        json={
            "fields": {
                "project": {"key": "GPUOPS"},
                "summary": f"XID {xid} on {node} GPU {gpu_id} - 自动恢复失败",
                "description": f"节点: {node}\nGPU: {gpu_id}\nXID: {xid}\n错误详情: {error_detail}",
                "issuetype": {"name": "Hardware Issue"},
                "priority": {"name": "High"}
            }
        },
        auth=("bot", "api-token")
    )
    requests.post(
        "https://hooks.slack.com/services/xxx/yyy/zzz",
        json={"text": f"[需人工介入] {node} GPU {gpu_id} XID {xid} 自动恢复失败，已创建 Jira 工单"}
    )
```

### 5.4 K8s 层实现要点

```yaml
# webhook-receiver Deployment
# 适用: K8s 1.31 / apps/v1 Deployment + v1 Service（均为稳定 API）
# 镜像: internal/gpu-self-heal:v1.2.0（内部镜像钉 tag，禁 latest）
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gpu-self-heal-webhook
  namespace: gpu-platform
spec:
  replicas: 2
  selector:
    matchLabels:
      app: gpu-self-heal
  template:
    metadata:
      labels:
        app: gpu-self-heal
    spec:
      serviceAccountName: gpu-self-heal
      containers:
        - name: webhook
          image: internal/gpu-self-heal:v1.2.0
          ports:
            - containerPort: 8080
          env:
            - name: KUBECONFIG
              value: ""              # 使用 ServiceAccount
            - name: SLACK_WEBHOOK
              valueFrom:
                secretKeyRef:
                  name: self-heal-secrets
                  key: slack-webhook
            - name: JIRA_URL
              value: "https://jira.internal"
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 5
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: gpu-self-heal-webhook
  namespace: gpu-platform
spec:
  selector:
    app: gpu-self-heal
  ports:
    - port: 80
      targetPort: 8080
---
# Alertmanager 配置片段（prometheus-operator AlertmanagerConfig）
# 适用: K8s 1.31 / monitoring.coreos.com/v1alpha1 AlertmanagerConfig（prometheus-operator v0.55+ 支持）
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: gpu-xid-alerts
  namespace: gpu-platform
spec:
  route:
    receiver: gpu-self-heal
    routes:
      - matchers:
          - name: alertname
            value: "GPUXidError"
        receiver: gpu-self-heal
  receivers:
    - name: gpu-self-heal
      webhookConfigs:
        - url: "http://gpu-self-heal-webhook.gpu-platform.svc:80/webhook/xid"
          sendResolved: true
```

### 5.5 自愈效果度量

```text
核心指标：
  自动处置率 = 自动解决的告警数 / 总告警数
    目标：> 60%（第一年）→ > 80%（第二年）

  MTTR（Mean Time To Repair）：
    自动处置 MTTR：目标 < 10 分钟
    人工处置 MTTR：目标 < 4 小时
    整体 MTTR：加权平均

  自愈成功率 = 自动处置后未复发（24h 内）的次数 / 自动处置总次数
    目标：> 90%

度量看板 PromQL（Prometheus 2.55 LTS；histogram_quantile 自 Prometheus 2.x 起稳定）：
  # 自动处置率（过去 7 天）
  count(self_heal_actions_total{result="resolved"})
    / count(self_heal_actions_total) * 100

  # MTTR（从告警触发到恢复的 P50/P95）
  histogram_quantile(0.50, rate(self_heal_duration_seconds_bucket[7d]))
  histogram_quantile(0.95, rate(self_heal_duration_seconds_bucket[7d]))
```

---

## 6 GitOps

### 6.1 ArgoCD ApplicationSet 管理 GPU 集群组件

```yaml
# argocd/gpu-platform-appset.yaml
# 适用: K8s 1.31 / ArgoCD v2.13.x / argoproj.io/v1alpha1 ApplicationSet（ApplicationSet 自 ArgoCD v2.3 起集成，v1alpha1 为当前稳定 API）
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: gpu-platform-components
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - component: gpu-operator
            path: infra/gpu-operator
            namespace: gpu-operator
            autoSync: "true"
          - component: volcano
            path: infra/volcano
            namespace: volcano-system
            autoSync: "true"
          - component: dcgm-exporter
            path: monitoring/dcgm-exporter
            namespace: monitoring
            autoSync: "true"
          - component: prometheus-stack
            path: monitoring/kube-prometheus-stack
            namespace: monitoring
            autoSync: "false"       # 监控栈手动同步（变更影响大）
          - component: kueue
            path: infra/kueue
            namespace: kueue-system
            autoSync: "true"
  template:
    metadata:
      name: "{{component}}"
    spec:
      project: gpu-platform
      source:
        repoURL: ssh://git@gitlab.example.com:2222/infra/gpu-platform.git
        targetRevision: main
        path: "{{path}}"
      destination:
        server: https://kubernetes.default.svc
        namespace: "{{namespace}}"
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
        syncOptions:
          - CreateNamespace=true
          - ServerSideApply=true
        retry:
          limit: 3
          backoff:
            duration: 5s
            factor: 2
            maxDuration: 3m
```

### 6.2 目录结构规范示例

```text
gpu-platform/                          # Git 仓库根目录
├── infra/
│   ├── gpu-operator/
│   │   ├── Chart.yaml                 # Helm Chart 元数据
│   │   ├── values.yaml                # 默认 values
│   │   ├── values-h100.yaml           # H100 集群覆盖
│   │   └── values-a100.yaml           # A100 集群覆盖
│   ├── volcano/
│   │   ├── Chart.yaml
│   │   └── values.yaml
│   └── kueue/
│       ├── Chart.yaml
│       └── values.yaml
├── monitoring/
│   ├── dcgm-exporter/
│   │   ├── Chart.yaml
│   │   └── values.yaml
│   ├── kube-prometheus-stack/
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   ├── dashboards/                # Grafana Dashboard JSON
│   │   │   ├── gpu-cluster-overview.json
│   │   │   ├── gpu-node-detail.json
│   │   │   └── gpu-utilization.json
│   │   └── rules/                     # PrometheusRule YAML
│   │       ├── gpu-alerts.yaml
│   │       └── gpu-recording-rules.yaml
│   └── loki/
│       ├── Chart.yaml
│       └── values.yaml
├── tenants/
│   ├── team-nlp/
│   │   ├── namespace.yaml
│   │   ├── resource-quota.yaml
│   │   ├── limit-range.yaml
│   │   └── local-queue.yaml
│   └── team-cv/
│       ├── namespace.yaml
│       ├── resource-quota.yaml
│       ├── limit-range.yaml
│       └── local-queue.yaml
├── scripts/
│   ├── ansible/                       # Ansible playbook 和 roles
│   ├── runbooks/                      # 运维 Runbook
│   └── ci/                            # CI 脚本
└── README.md
```

### 6.3 配置漂移检测

```bash
# 方法 1：ArgoCD 内置漂移检测（syncPolicy.automated.selfHeal: true）
# ArgoCD 每 3 分钟（默认）对比 Git 与集群实际状态，发现差异自动同步

# 方法 2：CI 管线中增加漂移检查
# .gitlab-ci.yml 片段
drift-check:
  stage: validate
  script:
    - argocd app diff gpu-operator --exit-code  # ArgoCD v2.13.x CLI；有差异时退出码非 0
    - argocd app diff volcano --exit-code
    - argocd app diff dcgm-exporter --exit-code
  allow_failure: false
  only:
    - schedules    # 每日定时检查

# 方法 3：Prometheus AlertRule 检测 ArgoCD OutOfSync（Prometheus 2.55 LTS，argocd_app_info 为 ArgoCD 内置 metrics）
- alert: ArgoCDAppOutOfSync
  expr: argocd_app_info{sync_status="OutOfSync"} == 1
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "ArgoCD Application {{ $labels.name }} 配置漂移超过 15 分钟"
```

---

## 7 巡检平台化

### 7.1 巡检 Agent DaemonSet 完整 YAML

```yaml
# gpu-inspection-agent.yaml
# 适用: K8s 1.31 / apps/v1 DaemonSet（v1 自 K8s 1.9 GA）
# 镜像: internal/gpu-inspection-agent:v1.0.0（内部镜像钉 tag，禁 latest）
# 依赖宿主机: nvidia-smi（driver R550+）、dcgmi（DCGM 3.3.9/4.2.x，通过 /var/run/dcgm.sock 访问）
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: gpu-inspection-agent
  namespace: gpu-platform
  labels:
    app: gpu-inspection-agent
spec:
  selector:
    matchLabels:
      app: gpu-inspection-agent
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 25%
  template:
    metadata:
      labels:
        app: gpu-inspection-agent
    spec:
      hostPID: true                    # 需要 hostPID 访问主机进程
      hostNetwork: false
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      serviceAccountName: gpu-inspection-agent
      containers:
        - name: inspection-agent
          image: internal/gpu-inspection-agent:v1.0.0
          securityContext:
            privileged: true           # 需要 privileged 访问 nvidia-smi / dcgmi
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: INSPECTION_INTERVAL
              value: "300"              # 每 5 分钟巡检一次
            - name: REPORT_ENDPOINT
              value: "http://gpu-inspection-collector.gpu-platform.svc:8080/api/v1/reports"
            - name: DCGM_DIAG_LEVEL
              value: "1"               # Level 1 = 快速诊断（Level 3 = 完整诊断）
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
              nvidia.com/gpu: "0"      # 巡检 agent 不占用 GPU（共享访问）
            limits:
              cpu: "500m"
              memory: "256Mi"
          volumeMounts:
            - name: nvidia-bin
              mountPath: /usr/bin/nvidia-smi
              readOnly: true
            - name: dcgm-socket
              mountPath: /var/run/dcgm.sock
            - name: host-dev
              mountPath: /host/dev
              readOnly: true
            - name: host-sys
              mountPath: /host/sys
              readOnly: true
            - name: host-proc
              mountPath: /host/proc
              readOnly: true
          livenessProbe:
            httpGet:
              path: /healthz
              port: 9090
            initialDelaySeconds: 10
            periodSeconds: 30
      volumes:
        - name: nvidia-bin
          hostPath:
            path: /usr/bin/nvidia-smi
            type: File
        - name: dcgm-socket
          hostPath:
            path: /var/run/dcgm.sock
            type: Socket
        - name: host-dev
          hostPath:
            path: /dev
        - name: host-sys
          hostPath:
            path: /sys
        - name: host-proc
          hostPath:
            path: /proc
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gpu-inspection-agent
  namespace: gpu-platform
---
apiVersion: v1
kind: Service
metadata:
  name: gpu-inspection-collector
  namespace: gpu-platform
spec:
  selector:
    app: gpu-inspection-collector
  ports:
    - port: 8080
      targetPort: 8080
```

### 7.2 健康分模型设计

```text
健康分计算：总分 100 分，各检查项按权重扣分

检查项权重打分表：

| 检查项 | 权重 | 扣分规则 | 最大扣分 |
|--------|------|---------|---------|
| GPU 在线（nvidia-smi 可达） | 25% | 任一 GPU 不可达 → 扣满分 | 25 |
| 温度正常（< 85°C） | 15% | 每超温 1 卡扣 5 分 | 15 |
| ECC 错误（UE = 0） | 15% | UE > 0 → 扣满分 | 15 |
| PCIe 带宽正常（Gen5 x16） | 10% | 降级（Gen4/Gen3）每卡扣 5 分 | 10 |
| DCGM diag 通过 | 15% | Level 1 失败 → 扣满分 | 15 |
| 驱动版本一致 | 5% | 版本不匹配 → 扣满分 | 5 |
| IB/RDMA 连通 | 10% | 端口 Down 或带宽 < 预期 80% → 扣满分 | 10 |
| 磁盘空间（< 90%） | 5% | > 90% 扣 3 分，> 95% 扣满分 | 5 |
| 时间同步（< 10ms） | 5% | 偏差 > 10ms 扣 3 分，> 100ms 扣满分 | 5 |
| Kubelet Ready | 5% | NotReady → 扣满分 | 5 |

健康分等级：
  90~100：健康（绿色）→ 正常运行
  70~89 ：亚健康（黄色）→ 计划维护窗口处理
  50~69 ：异常（橙色）→ 24 小时内处理
  < 50  ：严重故障（红色）→ 立即处理，隔离节点

健康分 PromQL（Prometheus 2.55 LTS / 自定义指标方式，需 gpu-inspection-agent 上报 gpu_node_health_score）：
  # 节点健康分
  gpu_node_health_score{node="gpu-h100-001"}

  # 集群平均健康分
  avg(gpu_node_health_score)

  # 不健康节点数
  count(gpu_node_health_score < 70)
```

---

## 8 平台门户功能清单

### 8.1 功能设计表

| 功能模块 | 用户操作 | 后端实现 | 权限控制 |
|---------|---------|---------|---------|
| 作业提交 | 填写镜像/卡数/优先级/训练脚本 → 提交 | 生成 Job/JobSet YAML → kubectl apply | Namespace 级别 RBAC |
| 配额查询 | 查看本团队配额使用/剩余/借用情况 | 查询 Kueue LocalQueue + ResourceQuota | 只能看本 namespace |
| 监控自助 | Grafana 嵌入式看板（按 namespace 过滤） | Grafana 多租户 + Dashboard 变量 | Grafana org/team 映射 |
| 日志查询 | 搜索本团队 Pod 日志 | Loki 按 namespace label 查询 | Loki 租户隔离 |
| 节点申请 | 申请独占节点（大模型训练） | 创建 NodeClaim → Cluster Autoscaler | 审批流（团队 Lead） |
| 作业管理 | 查看/停止/重启自己的作业 | K8s API + owner label 过滤 | 只能操作自己的作业 |
| 成本报表 | 查看本团队月度 GPU 卡时和费用 | 查询成本数据库按 namespace 聚合 | 只能看本团队 |

### 8.2 JupyterHub + KubeSpawner 部署要点

```yaml
# jupyterhub-values.yaml（Helm values 关键段）
# 适用: K8s 1.31 / JupyterHub Helm chart 3.3.x（示例，以官方 release 为准）/ KubeSpawner 7.x（chart 内含）
# helm install jupyterhub jupyterhub/jupyterhub --version 3.3.x（示例） -f jupyterhub-values.yaml
hub:
  config:
    Authenticator:
      admin_users:
        - admin-user
    JupyterHub:
      authenticator_class: generic-oauth
    GenericOAuthenticator:
      client_id: jupyterhub
      oauth_callback_url: https://jupyter.internal.company.com/hub/oauth_callback
      authorize_url: https://keycloak.internal.company.com/realms/gpu/protocol/openid-connect/auth
      token_url: https://keycloak.internal.company.com/realms/gpu/protocol/openid-connect/token
      userdata_url: https://keycloak.internal.company.com/realms/gpu/protocol/openid-connect/userinfo

singleuser:
  image:
    name: internal/jupyter-gpu
    tag: "pytorch-2.6-cuda12.8"  # 基线对齐：PyTorch 2.6 + CUDA 12.8（原稿 pytorch-2.3-cuda12.4 已过时，与基线不符）
  storage:
    type: dynamic
    capacity: 100Gi
    storageClassName: nfs-fast
    homeMountPath: /home/jovyan
  defaultUrl: /lab

  # Profile 列表：提供不同 GPU 规格供用户选择
  profileList:
    - display_name: "CPU Only（开发调试）"
      description: "2 CPU / 8GB RAM / 无 GPU"
      default: true
      kubespawner_override:
        cpu_limit: 2
        cpu_guarantee: 1
        mem_limit: "8G"
        mem_guarantee: "4G"
        node_selector:
          node-type: cpu-worker

    - display_name: "1× A100 80GB（小模型实验）"
      description: "8 CPU / 32GB RAM / 1× A100 80GB"
      kubespawner_override:
        cpu_limit: 8
        cpu_guarantee: 4
        mem_limit: "32G"
        mem_guarantee: "16G"
        extra_resource_limits:
          nvidia.com/gpu: "1"
        node_selector:
          nvidia.com/gpu.product: "NVIDIA-A100-SXM4-80GB"
        tolerations:
          - key: nvidia.com/gpu
            operator: Exists
            effect: NoSchedule

    - display_name: "2× A100 80GB（中等模型训练）"
      description: "16 CPU / 64GB RAM / 2× A100 80GB"
      kubespawner_override:
        cpu_limit: 16
        cpu_guarantee: 8
        mem_limit: "64G"
        mem_guarantee: "32G"
        extra_resource_limits:
          nvidia.com/gpu: "2"
        node_selector:
          nvidia.com/gpu.product: "NVIDIA-A100-SXM4-80GB"
        tolerations:
          - key: nvidia.com/gpu
            operator: Exists
            effect: NoSchedule

    - display_name: "8× H100 80GB（大规模训练）"
      description: "64 CPU / 256GB RAM / 8× H100 SXM"
      kubespawner_override:
        cpu_limit: 64
        cpu_guarantee: 32
        mem_limit: "256G"
        mem_guarantee: "128G"
        extra_resource_limits:
          nvidia.com/gpu: "8"
        node_selector:
          nvidia.com/gpu.product: "NVIDIA-H100-SXM"
        tolerations:
          - key: nvidia.com/gpu
            operator: Exists
            effect: NoSchedule

  # 空闲自动回收（防止 Jupyter 占用 GPU 不用）
  cull:
    enabled: true
    timeout: 3600          # 空闲 1 小时后自动关闭
    every: 300             # 每 5 分钟检查一次
    maxAge: 86400          # 最长运行 24 小时强制关闭
```

### 8.3 KServe / 推理网关要点

```yaml
KServe 部署要点：
  1. 依赖：Knative + Istio（或 Kourier 轻量网关）
  2. GPU 推理服务通过 InferenceService CRD 声明
  3. 自动 scale-to-zero（无请求时释放 GPU）
  4. 流量灰度：canary 部署（InferenceService.spec.canaryTrafficPercent）

InferenceService 示例（KServe v0.14.x，serving.kserve.io/v1beta1；依赖 Knative v1.14+ 或 Kourier 轻量网关）：
  apiVersion: serving.kserve.io/v1beta1
  kind: InferenceService
  metadata:
    name: llama3-70b
    namespace: team-nlp
  spec:
    predictor:
      minReplicas: 1
      maxReplicas: 4
      containerConcurrency: 8
      model:
        modelFormat:
          name: huggingface
        runtime: kserve-huggingface-runtime
        storageUri: s3://models/llama3-70b/
        resources:
          limits:
            nvidia.com/gpu: "4"       # Tensor Parallel = 4
            memory: "128Gi"
          requests:
            nvidia.com/gpu: "4"
            memory: "64Gi"
      nodeSelector:
        nvidia.com/gpu.product: "NVIDIA-H100-SXM"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule

推理网关架构要点：
  - 统一入口：Istio IngressGateway → KServe Router → Model Pods
  - 负载均衡：基于最少连接（least_request）而非轮询
  - 弹性伸缩：基于 GPU 利用率（而非 CPU/QPS）触发 HPA
  - 模型版本管理：InferenceService revision + traffic splitting
```

---

## 9 工具箱沉淀方法

### 9.1 脚本仓库目录结构规范

```text
gpu-ops-toolkit/                       # 运维工具仓库
├── README.md                          # 工具索引与快速入门
├── scripts/                           # 一次性 / 临时脚本
│   ├── gpu-health-check.sh            # 快速健康检查
│   ├── node-quarantine.sh             # 隔离故障节点
│   ├── checkpoint-cleanup.sh          # 清理过期 checkpoint
│   └── gpu-metrics-export.sh          # 导出 GPU 指标到 CSV
├── ansible/                           # Ansible playbook 和 roles
│   ├── inventory/
│   │   ├── production.ini
│   │   ├── staging.ini
│   │   └── group_vars/
│   ├── roles/
│   │   ├── nvidia-driver/
│   │   │   ├── tasks/main.yml
│   │   │   ├── handlers/main.yml
│   │   │   ├── vars/main.yml
│   │   │   └── templates/
│   │   ├── cuda-toolkit/
│   │   ├── containerd/
│   │   └── gpu-operator/
│   └── playbooks/
│       ├── gpu-inspection.yaml
│       ├── gpu-driver-upgrade.yaml
│       ├── cluster-bootstrap.yaml
│       └── node-decommission.yaml
├── runbooks/                          # 运维 Runbook（Markdown + 可执行）
│   ├── RB-001-gpu-xid-recovery.md
│   ├── RB-002-node-not-ready.md
│   ├── RB-003-rdma-link-down.md
│   └── RB-004-disk-full-cleanup.md
├── ci/                                # CI/CD 脚本
│   ├── .gitlab-ci.yml
│   ├── lint-ansible.sh
│   └── test-playbooks.sh
└── docs/                              # 文档
    ├── architecture.md
    └── runbook-index.md
```

### 9.2 脚本编写规范

```bash
#!/usr/bin/env bash
# ==============================================================================
# 脚本名称: gpu-health-check.sh
# 用途: 快速检查 GPU 节点健康状态
# 用法: ./gpu-health-check.sh [node_name] [--dry-run] [--json]
# 作者: ops-team
# 版本: 1.2.0
# ==============================================================================

set -euo pipefail

# ========== 全局变量 ==========
readonly SCRIPT_NAME="$(basename "$0")"
readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly LOG_FILE="/var/log/gpu-ops/${SCRIPT_NAME}.$(date +%Y%m%d).log"
readonly DRY_RUN="${DRY_RUN:-false}"
DRY_RUN_FLAG="false"
OUTPUT_JSON="false"
TARGET_NODE=""

# ========== 日志函数 ==========
log() {
  local level="$1"; shift
  local timestamp
  timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
  printf "[%s] [%-5s] %s\n" "$timestamp" "$level" "$*" | tee -a "$LOG_FILE"
}
log_info()  { log "INFO"  "$@"; }
log_warn()  { log "WARN"  "$@"; }
log_error() { log "ERROR" "$@"; }

# ========== 错误处理 ==========
cleanup() {
  local exit_code=$?
  if [ $exit_code -ne 0 ]; then
    log_error "脚本异常退出，退出码: $exit_code"
  fi
  # 清理临时文件
  rm -f /tmp/gpu-health-check-*.tmp
  exit $exit_code
}
trap cleanup EXIT
trap 'log_error "收到中断信号"; exit 130' INT TERM

# ========== 参数解析 ==========
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        DRY_RUN_FLAG="true"
        log_info "Dry-run 模式：不会执行任何变更操作"
        shift
        ;;
      --json)
        OUTPUT_JSON="true"
        shift
        ;;
      -h|--help)
        echo "用法: $0 [node_name] [--dry-run] [--json]"
        exit 0
        ;;
      -*)
        log_error "未知参数: $1"
        exit 1
        ;;
      *)
        TARGET_NODE="$1"
        shift
        ;;
    esac
  done
}

# ========== 主逻辑 ==========
main() {
  parse_args "$@"

  mkdir -p "$(dirname "$LOG_FILE")"
  log_info "开始 GPU 健康检查 (node=${TARGET_NODE:-all}, dry_run=${DRY_RUN_FLAG})"

  # ... 具体检查逻辑 ...
}

main "$@"
```

### 9.3 Runbook 即代码化思路

```text
传统 Runbook：
  Markdown 文档描述步骤，人工阅读执行
  问题：版本不一致、步骤遗漏、执行效率低

代码化 Runbook：
  Runbook = 可执行的脚本 + 决策逻辑 + 人工确认检查点
  每次执行自动记录日志和结果
  定期从执行日志中反馈优化 Runbook

实现方式：
  1. Runbook 仓库（Git）
     - 每个 RB-XXX 对应一个可执行脚本 + Markdown 说明
     - CI 自动测试 dry-run 模式

  2. 执行入口
     - 运维平台 UI 选择 Runbook → 执行（带 dry-run 预览）
     - 或直接命令行: ./runbooks/RB-001-gpu-xid-recovery.sh --node gpu-h100-005

  3. 执行审计
     - 每次执行记录：执行人、时间、参数、每步输出、最终结果
     - 存储到 PostgreSQL / Elasticsearch
     - 月度复盘：哪些 Runbook 使用频率最高、成功率如何、是否需要自动化

  4. 逐步自动化
     - Level 1：Runbook 半自动（脚本执行 + 人工确认关键步骤）
     - Level 2：Runbook 全自动（webhook 触发 + 自动执行 + 异常才人工介入）
     - Level 3：纳入自愈系统（完全无需人工触发）
```

# Go2 实时运行监控

这是一个独立、只读的 Linux Web 服务。它在 Go2/Jetson 上直接读取控制器产生的
`visloco_diag_*.csv`，并把 UWB、深度数据、发布速度、实际速度和推理性能展示在网页上。

Windows 不需要安装或运行任何程序，只使用浏览器访问：

```text
http://<Go2-IP>:8766
```

本项目不依赖 `deploy_assistant`，不提供启动、终止或控制机器狗的接口。

## 目录

```text
go2_runtime_monitor/
  server.py
  config.json
  run.sh
  static/
    index.html
    app.js
    style.css
  go2-runtime-monitor.service.example
```

## 1. 放到 Go2

将整个目录复制到 Go2，推荐位置：

```text
/home/unitree/Kaiwu-test/go2_runtime_monitor
```

如果在 Windows PowerShell 使用 `scp`，只是在复制文件，不需要运行 Windows 后端：

```powershell
scp -r D:\FWWB\go2_runtime_monitor unitree@10.168.1.100:/home/unitree/Kaiwu-test/
```

IP 地址按实际情况修改。

## 2. 检查配置

默认 CSV 路径是：

```text
/home/unitree/Kaiwu-test/sim2real_test_loco/deploy_loco/runtime/unitree_rl_lab_test/logs/loco/logs/visloco_diag_*.csv
```

如果 Go2 上的部署目录不同，修改 `config.json` 中的 `csv_globs` 和
`text_log_globs`。通配符支持 `*` 和递归 `**`。

主要配置：

| 字段 | 作用 |
|---|---|
| `host` / `port` | 服务监听地址，默认 `0.0.0.0:8766` |
| `csv_globs` | 诊断 CSV 搜索路径 |
| `text_log_globs` | 页面底部文本日志搜索路径 |
| `process_patterns` | 用 `pgrep -af` 检查的控制器进程 |
| `csv_stale_s` | CSV 超过多少秒没有刷新即判定为停止 |
| `uwb_stale_s` | UWB 数据过期阈值 |
| `depth_invalid_warn/bad` | 深度无效像素告警阈值 |
| `depth_stagnant_frames` | 深度统计连续不变多少帧后提示疑似卡帧/常量源 |

## 3. 启动

在 Go2 终端执行：

```bash
cd /home/unitree/Kaiwu-test/go2_runtime_monitor
chmod +x run.sh
./run.sh
```

看到下面的输出即表示服务已启动：

```text
Go2 runtime monitor listening on http://0.0.0.0:8766
Open from Windows: http://<GO2-IP>:8766
```

然后在 Windows 浏览器打开：

```text
http://10.168.1.100:8766
```

如需确认 Go2 的 IP：

```bash
hostname -I
```

## 4. 后台启动

临时后台运行：

```bash
cd /home/unitree/Kaiwu-test/go2_runtime_monitor
nohup ./run.sh > monitor.log 2>&1 &
```

查看监控服务自身日志：

```bash
tail -f /home/unitree/Kaiwu-test/go2_runtime_monitor/monitor.log
```

终止临时后台服务：

```bash
pkill -f 'go2_runtime_monitor/server.py'
```

## 5. 可选：注册 systemd 服务

只有需要开机自动启动时才执行：

```bash
sudo cp go2-runtime-monitor.service.example /etc/systemd/system/go2-runtime-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now go2-runtime-monitor
sudo systemctl status go2-runtime-monitor
```

查看服务日志：

```bash
journalctl -u go2-runtime-monitor -f
```

## 页面数据含义

- 理论速度：`theory_vx/vy/wz`，由 UWB/命令逻辑计算。
- 发布速度：`vx/vy/wz`，限幅和平滑后真正注入策略的速度命令。
- SportState 实测速度：`sport_vx/vy/vz/wz`。
- 综合实际速度：`feedback_v*`；页面会明确标注它来自 SportState 还是 UWB 估算。
- UWB：使用 `uwb_valid/age/error/enabled/channel` 判断状态。
- 深度：使用 `dep_inval/front_inval/front_min` 等字段判断数据质量。
- 推理：使用 `inference_ms/loop_ms/deadline_misses/consecutive_errors`。

所有折线图均支持交互：鼠标悬停图例会突出对应曲线；鼠标移到曲线附近会压暗其他
曲线，并显示同一帧所有序列的名称和数值。UWB 面板还会以机器狗为原点绘制目标的
相对方向、距离、最近轨迹和当前速度命令，旁边分别给出数据新鲜度、使能/错误和测距状态。
雷达采用机器狗机体系：页面上方是狗头前方，左侧是机器狗左侧；UWB 正 `beta` 和
机体 `+Y` 均绘制在页面左侧。

跑狗时不需要使用鼠标：速度曲线右端会常驻显示每条线的名称与当前值，理论速度使用
粗虚线覆盖显示；速度面板顶部的分层标记会始终显示理论、发布、实际三种速度的位置，
即使数值完全相同也不会互相遮住。面板还用近 50 帧 `vx` 平均绝对误差给出“跟速正常、
有偏差、异常”；当反馈来自 UWB 而不是 SportState 时，只会显示“仅 UWB 估算”。

## 深度相机状态说明

当前控制器 CSV 没有 `depth_frame_id` 或 `depth_frame_age`。因此页面上的“深度相机”状态是
根据深度统计是否合理、是否持续变化进行的只读推断：

- 能可靠发现全零、绝大部分无效、CSV 停止更新等异常。
- 连续相同会提示“疑似卡帧/常量源”。
- 不能百分之百区分真实相机和 `ConstantDepth`。

后续如果控制器增加 `depth_frame_id/depth_age_ms/depth_fps/depth_source` 字段，监控服务可以
直接使用这些字段做硬件级判定，但这不是运行当前监控项目的前置条件。

## 文本运行日志为空

实时状态和曲线来自 CSV，不依赖文本日志。如果页面底部提示没有找到文本日志，说明当前
控制程序的 stdout/stderr 只输出到了启动终端，或日志扩展名/目录不在 `text_log_globs` 中。
可以修改配置匹配实际日志文件；这不会影响 CSV 监控。

## 接口

全部为只读 GET 接口：

```text
GET /api/status       当前状态、最新样本和历史样本
GET /api/events       SSE 实时事件流
GET /api/logs         最近文本日志
```

服务没有写文件、执行控制命令或终止机器狗进程的 API。

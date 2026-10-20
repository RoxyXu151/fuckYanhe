# 产品评估与 v0.7 改进 / Product review

本评估基于源码、回归测试和本机浏览器验收。它描述单用户本机产品，不代表已经通过真实课堂准确率评测。English summary follows below.

## 定位与完整使用流程

核心价值是从有权访问的录播中发现重要通知，并让使用者回看原始证据。学习讲义是在同一份文字稿上增加的复习能力。通知检测、讲义整理和用户笔记分别保留，不能把模型概括当成教师原话。

完整流程：概览检查准备状态 → 配置转写和分析服务 → 连接校园平台或导入自己的字幕/媒体 → 保存课程、选择课次并批量处理 → 在队列处理失败与中断 → 回听报告证据 → 按章节复习、收藏原文、记笔记 → 下次从保存的位置继续。

## 发现的问题与已落实的改进

| 环节 | 原有问题 | v0.7 行为 |
| --- | --- | --- |
| 首次进入 | 课程列表不能解释下一步与环境是否准备好 | 概览显示使用步骤、模型状态、媒体工具、存储和待继续任务；演示不计入完成课次 |
| 模型配置 | 只能等任务失败才发现地址或鉴权问题 | 分别检测转写与分析服务，区分鉴权失败、限流、模型不存在与网络错误；不发送课程内容 |
| 重启 | 服务地址与模型名需要重新输入 | 可明确勾选记住非密钥配置；密钥不写入偏好文件，更换服务清除旧密钥 |
| 任务恢复 | 多个失败任务只能逐个继续 | 批量选择并继续，逐项返回结果；跳过完成/活动任务，防重复排队；校园任务提示重新连接 |
| 队列反馈 | 进度刷新会丢失勾选，讲义进度分散 | 保留仍可继续任务的勾选，队列同时展示未完成讲义任务 |
| 页面导航 | 刷新丢失所在课次，浏览器返回不可预期 | URL 记录所在课程、报告或听课页面；刷新与返回可恢复；无效地址回到概览 |
| 个人复习 | 无独立于 AI 的笔记与书签 | 本机保存笔记、原文书签和播放位置/速度；页面切换保留未保存草稿，离开前提示 |
| 原文变更 | 旧书签与进度可能定位到错误内容 | 比较文字稿指纹，标记旧进度和失效引用；保留个人笔记供核对 |
| 讲义重做 | 重新生成失败会丢失可阅读的完整版本 | 保留上一份完整讲义，可在文字稿匹配时恢复；恢复不调用模型 |
| 异步操作 | 慢请求可能覆盖后来打开的课次 | 课次切换、报告响应和笔记保存检查当前目标；保存期间的新编辑仍标为未保存 |
| 网络故障 | 服务中断容易表现为无响应 | 显示工作台离线提示；恢复后重新同步；页面会话更新可安全刷新校验信息 |
| 语言与外观 | 新增功能容易遗漏英文和暗色样式 | 新流程同时提供中英文，沿用亮/暗/系统主题；课程原文保持原语言 |

连接检测只读取模型列表。服务未实现此接口时显示“需实际任务验证”；模型列表成功也不证明音频分段或结构化生成契约已经通过。它不做计费推理，也不自动切换供应商。

## 架构取舍

- 保留 Python CLI + FastAPI + 原生网页，安装和运行不增加专用前端构建链。转写、通知、讲义分别缓存，换界面不重跑模型。
- 文件式原子写入适合一人本机使用；重试、笔记和讲义操作在进程内加锁。尚未为多进程并发、多租户和公网部署设计数据库、账户体系或隔离边界。
- 浏览器仅保存语言和主题；课程、书签、笔记、进度在本机私有运行目录。可选模型偏好不包含密钥。校园登录与手填模型密钥在服务端会话内存中。
- 本次优先修复持续使用的断点和失败恢复，不添加需要额外外部权限的日历、消息推送或自动签到。

## 验收依据

自动测试覆盖字幕解析、全覆盖分块、通知证据校验、媒体解码、授权访问、缓存恢复、课程管理、混合模型服务、讲义引用和本次新增的偏好/笔记/书签/恢复/批量重试。云端与 GPU 模型响应采用受控样例；测试通过数是软件回归结果，不能换算为课堂召回率。

浏览器检查包括：概览与环境状态、中英文和主题切换、未登录批量恢复的明确反馈、切换页面后保留草稿、笔记与原文书签跨刷新保存、听课深链接恢复，以及继续保存的播放位置和速度。验收用临时笔记和书签检查后清除。真实媒体和截图不随源码发布。

确切测试结果记录在对应 GitHub Release。远程 Actions 尚未启用，仓库提供可复制的工作流样例；本机测试成功不应描述为远程 CI 成功。

## 仍有边界与下一步

1. **准确率仍需独立人工评估。** 尚无至少十节独立、完整人工标注的真实课堂测试集，不能宣称高风险事项达到 95% 召回率。下阶段应分别记录考勤、作业、考试的漏报/误报和证据误差。
2. **讲义可能存在语义和公式错误。** 校验引用存在不等于证明概括正确；已明确区分 AI 概括与原文，并支持个人修正笔记。未来可增加逐点反馈和人工修订导出。
3. **视觉通路有采样局限。** 可选帧分析不等于完整 PPT 提取、逐帧 OCR 或视觉召回保证。原始课件文件仍需另行获取。
4. **运行数据目前依赖文件备份。** 未提供完整的一键备份/导入迁移、磁盘清理回收站或跨设备同步；升级前备份私有运行目录，不能把它上传公开仓库。
5. **长任务依赖进程。** 重启保留成功块，但需人工恢复和校园重新登录。尚无无人值守系统服务、分布式调度或多机故障转移。

这些边界不影响本机课程分析和复习闭环，但决定了当前适合个人使用与反馈，尚不能宣传为可替代听课的成熟教学服务。

## English summary

The product is a single-user local workspace for evidence-backed lecture notices and transcript-based study. Version 0.7 adds an onboarding overview, read-only model checks, optional non-secret model preferences, selected-task recovery, study tasks in the queue, restorable URLs, private notes and source bookmarks, playback progress, and recovery of a previous complete note version. Selection and asynchronous navigation races are handled without rerunning completed work.

Keys are excluded from preferences, and credentials are cleared when service identity changes. Connection checks send no lecture content and do not certify inference compatibility. Study records stay in private runtime storage; changed transcripts mark old positions and citations as stale. Chinese/English and light/dark/system appearances cover the new workflow.

Regression tests and browser checks verify software behavior; controlled model fixtures do not establish real classroom accuracy. A representative independent human-labelled evaluation set, safer data backup/migration, and richer manual note corrections remain future work. Generated formulas and summaries require review against original evidence. The application is not a public multi-user service. See the release for exact local test results; hosted CI is not enabled.

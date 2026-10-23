# Automatic ASR alignment repair

The self-hosted Qwen service retries failed forced-Chinese alignment with native language detection, so language/no-speech metadata is parsed by the model rather than treated as a word transcript. Pure punctuation is not a spoken-word cue. Remaining invalid windows are subdivided with real audio overlap, and each returned aligner span is assigned to one core by midpoint. No equal-duration or synthetic timestamps are generated. Unrecoverable speech still returns a failure.

The workbench retries partial and failed Qwen ASR jobs automatically, using the existing successful-chunk cache. Already prepared recordings go directly to the bounded analysis queue. Retries are limited to two per repair-policy version and persisted in job metadata; restarting does not reset this limit. Explicitly canceled tasks, completed jobs, and paid cloud profiles are excluded. Failed notice-analysis windows continue to use their existing validated cache/retry path.

运行中的课程继续使用所配置数据目录中的缓存。Qwen 时间戳失败时，先尝试语言自动检测，再只缩短失败窗口；时间戳只能来自对齐器。工作台会自动补齐缺失的转写块，已下载录像直接进入分析队列。每个任务每个修复版本最多自动补齐两轮；仍无法对齐时保留部分报告和失败提示，不无限重试，也不会把失败改成“未发现事项”。

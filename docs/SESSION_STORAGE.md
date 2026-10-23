# Saved school sessions / 校园会话保存

Windows builds store a verified school session token in Windows Credential Manager, scoped to the current Windows user and resolved workspace path. The app does not collect or save the school password and has no plaintext-token fallback. Model API credentials are unaffected.

Windows 版本把已验证的校园会话令牌保存到 Windows 凭据管理器，按当前用户及工作区隔离；不收集或保存学校密码，不使用明文令牌文件作为替代。模型服务密钥的处理方式不受影响。

Official sign-in uses a dedicated persistent browser profile under the selected data directory: `private/official-browser`. It contains private browser session state and must not be published or included in report exports. Password saving is disabled in this profile. On Windows its permissions are restricted to the current user, SYSTEM and Administrators.

官方登录使用数据目录下的专用持久浏览器目录 `private/official-browser`。该目录包含私人登录状态，不得开源或随报告导出；密码保存已关闭，并限制 Windows 文件访问权限。

On startup, verification runs in the background against the official user endpoint. A valid session resumes pending downloads without opening a login window. An explicit authentication-expiry response removes the saved token. Network errors, rate limits and permission failures retain it and do not claim that the login has expired. The workspace retries validation when polled after the retry interval. Downloaded media can still be analyzed independently.

重启时后台向官方验证保存的会话：有效则恢复队列，明确过期则清除令牌并提示重新登录；网络、限流或课程权限问题不会误当作过期。工作台轮询会在重试间隔后重新验证。已有媒体的分析不依赖重新登录。

Signing out clears the saved Credential Manager entry and the workspace's in-memory connection. A later explicit connection can still reuse the official browser's own state. Moving to a different resolved data directory creates a separate credential target.

退出工作台登录会清除凭据管理器条目及工作台内存连接；以后主动连接时仍可利用官方浏览器自身保留的登录状态。更换数据目录会使用不同的凭据条目。

""" P0 保命：备份 / 恢复 / 维护记录 / 定时备份。

- backup.py    一致性快照（VACUUM INTO）、manifest、校验、保留策略
- restore.py   运行期排队、启动期执行
- mlog.py      maintenance_log 读写（失败只记日志，绝不抛给业务）
- service.py   "备份 + 写记录 + 清理"的组合入口
- scheduler.py 定时备份循环
- routes.py    /api/maintenance/*

安全：备份含明文模型密钥。本包任何日志只打备份名/路径/字节数，绝不打 manifest 之外的库内容。
"""

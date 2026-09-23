# 半导体条款生效索引

工程用于保存标准条款与产品适用性判定的本地数据。数据文件采用 SQLite，迁移脚本可重复执行，接口进程默认监听 8080 端口。当前仅保留健康检查和数据库连通性测试，便于后续加入条款版本规则。

启动：`docker build -t standards-index . && docker run --rm -p 8080:8080 standards-index`。本地测试：`pytest`。

## 开发检查

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest`
- 编译检查：`python3 -m compileall -q app.py`

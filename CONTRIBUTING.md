# CONTRIBUTING

欢迎来改。几条项目纪律（都是踩坑换来的）：

1. **聊天层神圣不可侵犯**：语音对话手感是第一生命线，任何改动不得降低对话流畅度。
2. **意图识别归模型**：新需求先想"能不能交给模型判断"，机械正则只做安全召回保险丝；
   不得不加的机械兜底件登记进 `plugins/scaffold.py`（临时脚手架，挂账待拆，可热插拔）。
3. **插件化**：新能力写成 `plugins/` 插件经 kernel 装配，不往 companion_rt 堆全局函数。
4. **凭据零落盘**：key 只走 `secrets_store.py`（keyring/环境变量），config.json 不写 key。
5. **测试**：改动必跑对应测试（`python tests/test_x.py`，无 pytest；全链路仿真
   `python tools/task_sim.py`）；前台 GUI 实测用 `tools/benchmark_gui.py`（占屏，跑前确认没人用机器）。
6. **危险操作过闸**：删/发/付/系统变更一律走确认链，宁可误拦不放过。
7. 外部协议/框架的字段假设，先写探针实证再写代码——凭文档猜是踩坑大户。

# 受控出口代理契约（Task 5）

broker 不会自行直连任何供应商。生产启动时必须显式提供出口代理与策略：

```powershell
python deploy/multiuser/egress_proxy.py `
  --listen 127.0.0.1 --port 8899 `
  --policy deploy/multiuser/egress-policy.json

python -m easel serve-control `
  --control-dir ... --public-origin https://easel.example `
  --master-key-file ... `
  --egress-proxy http://127.0.0.1:8899 `
  --egress-policy deploy/multiuser/egress-policy.json
```

缺失代理或策略时，broker 请求明确返回 `503 egress_unavailable`，不存在
silent direct fallback；测试只能通过显式注入 fake transport 绕过网络。

## 代理行为

- 只接受 `CONNECT host:443`；其他方法、明文 HTTP、非 443 端口一律拒绝。
- 在连接阶段解析目标主机，校验解析出的**全部** IPv4/IPv6 地址；任一地址
  属于回环、私网、链路本地、保留、组播、未指定或云元数据段即整体拒绝。
- 校验后直接连接解析出的 IP，不在代理内再次解析，避免 DNS 重绑定。
- 不跟随重定向；broker 收到 3xx 响应会返回 `502 upstream_redirect`，
  不会把上游 Key 发往跳转目标。
- 管理员例外只能写在本策略文件中，member 没有修改接口：

```json
{
  "approved_hosts": ["internal-provider.example"],
  "approved_cidrs": ["10.20.0.0/16"]
}
```

例外按精确主机名/网段匹配；相似域名不会继承。策略文件缺失、含未知字段
或非法 CIDR 时代理启动失败。

## 真实门槛

普通单元测试不联网。真实出口验收必须显式选择：

```powershell
$env:EASEL_EGRESS_LIVE = "1"
$env:EASEL_TEST_EGRESS_PROXY = "http://127.0.0.1:8899"
python -m pytest tests/integration/multiuser/test_egress.py -q
```

要求 Linux 或具备 IPv4/IPv6 出口的环境；显式选择但缺少代理地址时测试
直接失败，不把 skip 当通过。当前 Windows 开发机未执行该门槛，报告中如实
记录为“未执行”。

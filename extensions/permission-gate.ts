// 唯一扩展入口；策略与处理器集中在 permission-policy.ts，避免目录自动加载时重复挂闸。
export { installPermissionGate as default } from "./permission-policy.ts";

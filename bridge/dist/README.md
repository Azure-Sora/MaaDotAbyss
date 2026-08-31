# 预构建桥插件

CI 构建不了桥：编译依赖本机游戏目录的 BepInEx interop 程序集（游戏文件不可 redistribution），
所以本目录的 `DotAbyssBridge.dll` 由本机构建后提交，Release 工作流原样打进发布包。

更新流程：改 `bridge/DotAbyssBridge/` 下 C# 源码 → `dotnet build bridge/DotAbyssBridge -c Release`
→ 用 `bridge/DotAbyssBridge/bin/Release/net6.0/DotAbyssBridge.dll` 覆盖本目录同名文件并提交。

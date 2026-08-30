using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;

namespace DotAbyssBridge;

/// <summary>
/// 游戏内桥插件（docs/research/13 §2）：在游戏进程内开 127.0.0.1 HTTP 服务，
/// 提供 /ping /screenshot /ui /click /click_at —— 输入零焦点依赖、截图进程内直读。
/// </summary>
[BepInPlugin(GUID, NAME, VERSION)]
public sealed class BridgePlugin : BasePlugin
{
    public const string GUID = "local.dotabyss.bridge";
    public const string NAME = "DotAbyssBridge";
    public const string VERSION = "0.2.0";

    internal new static ManualLogSource Log;
    internal static ConfigEntry<int> Port;

    public override void Load()
    {
        Log = base.Log;
        Port = Config.Bind("General", "Port", 27124, "桥 HTTP 服务端口（仅监听 127.0.0.1）");

        ClassInjector.RegisterTypeInIl2Cpp<BridgeBehaviour>();
        var host = new GameObject("DotAbyssBridgeHost");
        Object.DontDestroyOnLoad(host);
        host.AddComponent(Il2CppType.Of<BridgeBehaviour>());
        Log.LogInfo($"{NAME} {VERSION} loaded");
    }
}

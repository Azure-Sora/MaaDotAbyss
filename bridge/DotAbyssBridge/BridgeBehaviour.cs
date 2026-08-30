using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using System.Threading.Tasks;
using Il2CppInterop.Runtime.InteropTypes.Arrays;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

namespace DotAbyssBridge;

/// <summary>
/// 桥宿主组件：Update 泵主线程任务队列与截图状态机；HTTP 服务线程只做 IO，
/// 所有 Unity API 访问经 MainThreadJobs 投递回主线程。
/// </summary>
public sealed class BridgeBehaviour : MonoBehaviour
{
    internal readonly ConcurrentQueue<Action> MainThreadJobs = new();

    private BridgeServer _server;

    // 截图状态机：0=空闲 1=本帧发起抓取 2=等帧回读
    private int _captureState;
    private int _captureFrames;
    private Texture2D _captureTex;
    private TaskCompletionSource<byte[]> _pngWaiter;

    // Il2CppInterop 注入类的魔术方法按名字挂钩（非 override）
    private void Awake()
    {
        _server = new BridgeServer(this, BridgePlugin.Port.Value);
        _server.Start();
    }

    private void OnDestroy()
    {
        _server?.Stop();
        _server = null;
    }

    private void Update()
    {
        while (MainThreadJobs.TryDequeue(out var job))
        {
            try { job(); }
            catch (Exception e) { BridgePlugin.Log.LogWarning($"main job: {e.Message}"); }
        }
        PumpCapture();
    }

    // ---- 截图（主线程状态机） ------------------------------------------

    /// <summary>HTTP 线程调用：请求下一两帧后的整屏 PNG。</summary>
    public Task<byte[]> CapturePngAsync()
    {
        if (_captureState != 0 || _pngWaiter is { Task.IsCompleted: false })
            throw new InvalidOperationException("截图进行中");
        _pngWaiter = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        _captureState = 1;
        return _pngWaiter.Task;
    }

    private void PumpCapture()
    {
        if (_captureState == 1)
        {
            // CaptureScreenshotAsTexture 的内容到下一帧才有效，等两帧稳妥
            _captureTex = ScreenCapture.CaptureScreenshotAsTexture();
            _captureFrames = 2;
            _captureState = 2;
        }
        else if (_captureState == 2 && --_captureFrames <= 0)
        {
            _captureState = 0;
            var waiter = _pngWaiter;
            _pngWaiter = null;
            byte[] png;
            try { png = EncodeCapture(_captureTex); }
            catch (Exception e)
            {
                waiter?.TrySetException(e);
                return;
            }
            _captureTex = null;
            waiter?.TrySetResult(png);
        }
    }

    private static byte[] EncodeCapture(Texture2D tex)
    {
        if (tex == null) throw new InvalidOperationException("截图纹理为空（游戏未渲染？）");
        // 实测（2026-08-30 与 MAA WGC 逐像素对照）：CaptureScreenshotAsTexture 的
        // GetPixels32 行序已是自顶向下，直接编码即与屏幕一致——不要再做任何翻转
        var pngIl = ImageConversion.EncodeToPNG(tex);
        var png = new byte[pngIl.Length];
        for (int i = 0; i < png.Length; i++) png[i] = pngIl[i];
        return png;
    }

    // ---- UI 树导出（主线程） -------------------------------------------

    internal string BuildUiJson(int maxNodes = 4000, int maxDepth = 40, string canvasFilter = null)
    {
        var sb = new StringBuilder(1 << 16);
        sb.Append("{\"scene\":\"").Append(Esc(SceneManager.GetActiveScene().name))
          .Append("\",\"canvases\":[");
        var canvases = UnityEngine.Object.FindObjectsOfType<Canvas>();
        bool first = true;
        int count = 0;
        foreach (var c in canvases)
        {
            if (c == null || !c.gameObject.activeInHierarchy) continue;
            if (canvasFilter != null && c.gameObject.name != canvasFilter) continue;
            if (!first) sb.Append(',');
            first = false;
            Walk(c.transform, 0, sb, ref count, maxNodes, maxDepth, c.worldCamera);
            if (count >= maxNodes) break;
        }
        sb.Append("]}");
        return sb.ToString();
    }

    private void Walk(Transform tr, int depth, StringBuilder sb, ref int count, int maxNodes, int maxDepth, Camera cam)
    {
        count++;
        sb.Append("{\"name\":\"").Append(Esc(tr.gameObject.name)).Append('"');
        sb.Append(",\"active\":").Append(tr.gameObject.activeSelf ? "true" : "false");
        var rt = tr.TryCast<RectTransform>();
        if (rt != null)
        {
            sb.Append(",\"pos\":[").Append((int)rt.anchoredPosition.x).Append(',').Append((int)rt.anchoredPosition.y).Append(']');
            sb.Append(",\"size\":[").Append((int)rt.rect.width).Append(',').Append((int)rt.rect.height).Append(']');
            AppendScreenBox(rt, cam, sb);
        }
        var tmp = tr.GetComponent<TMPro.TMP_Text>();
        if (tmp != null && !string.IsNullOrEmpty(tmp.text))
            sb.Append(",\"text\":\"").Append(Esc(tmp.text)).Append('"');

        var btn = tr.GetComponent<Button>();
        if (btn != null)
        {
            sb.Append(",\"button\":{\"interactable\":").Append(btn.interactable ? "true" : "false");
            sb.Append(",\"path\":\"").Append(Esc(NodePath(tr))).Append("\"}");
        }

        sb.Append(",\"children\":[");
        if (depth < maxDepth && count < maxNodes)
        {
            bool first = true;
            for (int i = 0; i < tr.childCount; i++)
            {
                var child = tr.GetChild(i);
                if (child == null) continue;
                if (!first) sb.Append(',');
                first = false;
                Walk(child, depth + 1, sb, ref count, maxNodes, maxDepth, cam);
                if (count >= maxNodes) break;
            }
        }
        sb.Append("]}");
    }

    /// <summary>RectTransform 四角 → 屏幕包围盒（图像坐标，y 自顶向下），供模板匹配命中 → 按钮路径映射。</summary>
    private static void AppendScreenBox(RectTransform rt, Camera cam, StringBuilder sb)
    {
        var corners = new Il2CppStructArray<Vector3>(4);
        rt.GetWorldCorners(corners);
        float minX = float.MaxValue, minY = float.MaxValue, maxX = float.MinValue, maxY = float.MinValue;
        for (int i = 0; i < 4; i++)
        {
            var sp = RectTransformUtility.WorldToScreenPoint(cam, corners[i]);
            if (sp.x < minX) minX = sp.x;
            if (sp.y < minY) minY = sp.y;
            if (sp.x > maxX) maxX = sp.x;
            if (sp.y > maxY) maxY = sp.y;
        }
        float hh = Screen.height;
        sb.Append(",\"screen\":[").Append((int)minX).Append(',').Append((int)(hh - maxY))
          .Append(',').Append((int)maxX).Append(',').Append((int)(hh - minY)).Append(']');
    }

    private static string NodePath(Transform tr)
    {
        var sb = new StringBuilder();
        var cur = tr;
        while (cur != null)
        {
            sb.Insert(0, "/" + cur.gameObject.name);
            cur = cur.parent;
        }
        return sb.ToString();
    }

    // ---- 点击（主线程） -------------------------------------------------

    /// <summary>按路径（精确或名称包含）找 Button 并触发 onClick。</summary>
    public string ClickByPath(string path)
    {
        var btn = FindButton(path);
        if (btn == null) throw new KeyNotFoundException($"未找到按钮: {path}");
        btn.onClick.Invoke();
        return NodePath(btn.transform);
    }

    /// <summary>
    /// 射线式真实点击：EventSystem.RaycastAll 取命中点最上层对象，沿层级找
    /// IPointerClickHandler 触发（与真实 uGUI 点击同路径）。弹窗按钮普遍不是
    /// Button 组件（ゲットキー消費实测），ClickAtPoint 只认 Button 会穿透弹窗，
    /// 本方法补足。
    /// </summary>
    public string PointerClickAt(int x, int y)
    {
        int py = Screen.height - y;
        var es = UnityEngine.EventSystems.EventSystem.current;
        if (es == null) throw new InvalidOperationException("无 EventSystem");
        var pd = new UnityEngine.EventSystems.PointerEventData(es)
        {
            position = new Vector2(x, py),
            button = UnityEngine.EventSystems.PointerEventData.InputButton.Left,
        };
        var results = new Il2CppSystem.Collections.Generic.List<UnityEngine.EventSystems.RaycastResult>();
        es.RaycastAll(pd, results);
        if (results.Count == 0)
            throw new KeyNotFoundException($"({x},{y}) 无 UI 命中");
        var go = results[0].gameObject;
        string hitPath = NodePath(go.transform);
        var target = UnityEngine.EventSystems.ExecuteEvents.GetEventHandler<UnityEngine.EventSystems.IPointerClickHandler>(go);
        if (target == null)
            throw new KeyNotFoundException($"({x},{y}) 命中 {go.name}，但无 PointerClick 处理器");
        UnityEngine.EventSystems.ExecuteEvents.Execute<UnityEngine.EventSystems.IPointerClickHandler>(
            target, pd, UnityEngine.EventSystems.ExecuteEvents.pointerClickHandler);
        return hitPath;
    }

    /// <summary>找点 (x,y) 下面积最小的可交互 Button 并触发。坐标为截图像素系（y 自顶向下）。</summary>
    public string ClickAtPoint(int x, int y)
    {
        // 图像 y（向下）→ Unity 屏幕 y（向上）
        int py = Screen.height - y;
        Button best = null;
        float bestArea = float.MaxValue;
        string bestPath = null;
        var buttons = UnityEngine.Object.FindObjectsOfType<Button>();
        foreach (var b in buttons)
        {
            if (b == null || !b.interactable || !b.gameObject.activeInHierarchy) continue;
            var rt = b.transform.TryCast<RectTransform>();
            if (rt == null) continue;
            var canvas = b.GetComponentInParent<Canvas>();
            var cam = canvas != null ? canvas.worldCamera : null;
            var corners = new Il2CppStructArray<Vector3>(4);
            rt.GetWorldCorners(corners);
            float minX = float.MaxValue, minY = float.MaxValue, maxX = float.MinValue, maxY = float.MinValue;
            for (int i = 0; i < 4; i++)
            {
                var sp = RectTransformUtility.WorldToScreenPoint(cam, corners[i]);
                if (sp.x < minX) minX = sp.x;
                if (sp.y < minY) minY = sp.y;
                if (sp.x > maxX) maxX = sp.x;
                if (sp.y > maxY) maxY = sp.y;
            }
            if (x < minX || x > maxX || py < minY || py > maxY) continue;
            float area = (maxX - minX) * (maxY - minY);
            if (area < bestArea)
            {
                bestArea = area;
                best = b;
                bestPath = NodePath(b.transform);
            }
        }
        if (best == null) throw new KeyNotFoundException($"({x},{y}) 下没有可交互按钮");
        best.onClick.Invoke();
        return bestPath;
    }

    private static Button FindButton(string pathOrName)
    {
        Button contains = null;
        var buttons = UnityEngine.Object.FindObjectsOfType<Button>();
        foreach (var b in buttons)
        {
            if (b == null || !b.gameObject.activeInHierarchy) continue;
            var p = NodePath(b.transform);
            if (p == pathOrName) return b;
            if (contains == null && b.gameObject.name.Contains(pathOrName)) contains = b;
        }
        return contains;
    }

    internal static string Esc(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.ToString();
    }
}

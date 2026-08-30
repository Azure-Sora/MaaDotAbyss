// 枚举 interop 程序集的类型清单（找游戏 UI 类名用）。
// 用法: dotnet run -- <interop目录> <输出txt> [关键词1 关键词2 ...]
// 输出: 每行 "程序集  命名空间.类型 : 基类"；有关键词时只输出匹配行（大小写不敏感）。
using System.Reflection;

var dir = args[0];
var outFile = args[1];
var keywords = args.Skip(2).Select(k => k.ToLowerInvariant()).ToArray();

var lines = new List<string>();
foreach (var dll in Directory.GetFiles(dir, "*.dll").OrderBy(p => p, StringComparer.OrdinalIgnoreCase))
{
    Assembly asm;
    try { asm = Assembly.LoadFrom(dll); }
    catch (Exception e) { Console.Error.WriteLine($"[skip load] {Path.GetFileName(dll)}: {e.Message}"); continue; }

    Type[] types;
    try { types = asm.GetTypes(); }
    catch (ReflectionTypeLoadException ex) { types = ex.Types.Where(t => t is not null).ToArray(); }
    catch (Exception e) { Console.Error.WriteLine($"[skip types] {Path.GetFileName(dll)}: {e.Message}"); continue; }

    var asmName = asm.GetName().Name;
    foreach (var t in types)
    {
        string full, baseName;
        try
        {
            var ns = t.Namespace ?? "";
            full = (string.IsNullOrEmpty(ns) ? "" : ns + ".") + t.Name;
            baseName = t.BaseType is { } b ? b.Name : "";
        }
        catch { continue; }  // 缺依赖程序集时元数据访问会炸，跳过该类型
        if (keywords.Length > 0 && !keywords.Any(k => full.ToLowerInvariant().Contains(k)))
            continue;
        lines.Add($"{asmName}\t{full}\t{baseName}");
    }
}
File.WriteAllLines(outFile, lines);
Console.WriteLine($"written {lines.Count} types -> {outFile}");

// The command protocol of phoneuse's hid_daemon.py (run_command), on Windows.
// One command per line, one reply per line ("ok ..." or "err ...").
using System.Globalization;

namespace PhoneHid;

sealed class Daemon(BleHid hid)
{
    static readonly Dictionary<string, byte> Mods = new()
    {
        ["ctrl"] = 0x01, ["shift"] = 0x02, ["alt"] = 0x04, ["gui"] = 0x08, ["cmd"] = 0x08,
        ["rctrl"] = 0x10, ["rshift"] = 0x20, ["ralt"] = 0x40, ["rgui"] = 0x80
    };
    static readonly Dictionary<string, byte> Named = new()
    {
        ["enter"] = 40, ["return"] = 40, ["esc"] = 41, ["escape"] = 41, ["backspace"] = 42, ["tab"] = 43,
        ["space"] = 44, ["right"] = 79, ["left"] = 80, ["down"] = 81, ["up"] = 82, ["delete"] = 76,
        ["home"] = 74, ["end"] = 77, ["pageup"] = 75, ["pagedown"] = 78
    };
    static readonly Dictionary<string, int> ConsumerKeys = new() { ["eject"] = 0, ["siri"] = 1, ["home"] = 2, ["search"] = 3 };
    static readonly Dictionary<string, byte> Buttons = new() { ["left"] = 1, ["right"] = 2, ["middle"] = 4 };
    static readonly Dictionary<char, (byte Code, byte Mods)> Chars = BuildChars();

    static Dictionary<char, (byte, byte)> BuildChars()
    {
        var m = new Dictionary<char, (byte, byte)>();
        var letters = "abcdefghijklmnopqrstuvwxyz";
        for (var i = 0; i < 26; i++) { m[letters[i]] = ((byte)(4 + i), 0); m[char.ToUpper(letters[i])] = ((byte)(4 + i), 2); }
        var digits = "1234567890"; var shifted = "!@#$%^&*()";
        for (var i = 0; i < 10; i++) { m[digits[i]] = ((byte)(30 + i), 0); m[shifted[i]] = ((byte)(30 + i), 2); }
        foreach (var (p, s, c) in new[] { ('-', '_', 45), ('=', '+', 46), ('[', '{', 47), (']', '}', 48), ('\\', '|', 49),
                     (';', ':', 51), ('\'', '"', 52), ('`', '~', 53), (',', '<', 54), ('.', '>', 55), ('/', '?', 56) })
        { m[p] = ((byte)c, 0); m[s] = ((byte)c, 2); }
        m[' '] = (44, 0); m['\n'] = (40, 0); m['\t'] = (43, 0);
        return m;
    }

    readonly SemaphoreSlim _gate = new(1, 1);
    byte _buttons;
    byte _consumerBits;

    public async Task<string> Run(string line)
    {
        await _gate.WaitAsync();
        try { return await RunLocked(line); }
        catch (Exception e) { return "err " + e.Message; }
        finally { _gate.Release(); }
    }

    async Task<string> RunLocked(string line)
    {
        var trimmed = line.Trim();
        var sp = trimmed.IndexOf(' ');
        var cmd = (sp < 0 ? trimmed : trimmed[..sp]).ToLowerInvariant();
        var rest = sp < 0 ? "" : trimmed[(sp + 1)..];
        // "type" keeps its text as-is (spaces included); split everything else.
        if (cmd == "type") rest = line.TrimStart()[(Math.Min(line.TrimStart().Length, 5))..].TrimEnd('\r', '\n');
        var args = rest.Split(' ', StringSplitOptions.RemoveEmptyEntries);

        switch (cmd)
        {
            case "status":
                return $"ok connected={(hid.Connected ? "True" : "False")} released=False peer={hid.Peer} paired=[] mirror=None";
            case "connect": case "release": case "mirror": case "pair": case "bridge":
                return "ok " + cmd + " (not needed on Windows)";
            case "eject":
                await Consumer("eject", 0.05);
                return "ok";
            case "consumer":
                if (args.SequenceEqual(["up"])) { _consumerBits = 0; await hid.Send(4, [0]); return "ok"; }
                if (args.Length == 0 || !ConsumerKeys.ContainsKey(args[0]))
                    return "err consumer keys: " + string.Join(", ", ConsumerKeys.Keys);
                var how = args.Length > 1 ? args[1] : "0.05";
                if (how is "down" or "up") await ConsumerSet(args[0], how == "down");
                else await Consumer(args[0], F(how));
                return "ok";
            case "a":
            {
                var b = args.Length > 2 ? (byte)(int.Parse(args[2]) & 7) : (byte)0;
                await hid.Send(3, Absolute(F(args[0]), F(args[1]), b));
                return "ok";
            }
            case "m": case "md":
            {
                var dx = Clamp(int.Parse(args[0])); var dy = Clamp(int.Parse(args[1]));
                var wheel = args.Length > 2 ? Clamp(int.Parse(args[2])) : 0;
                if (args.Length > 3) _buttons = (byte)(int.Parse(args[3]) & 7);
                await Mouse(dx, dy, wheel);
                return "ok";
            }
            case "k":
            {
                var report = new byte[8];
                report[0] = (byte)(int.Parse(args[0]) & 0xFF);
                for (var i = 1; i < args.Length && i <= 6; i++) report[i + 1] = (byte)(int.Parse(args[i]) & 0xFF);
                await hid.Send(1, report);
                return "ok";
            }
            case "move":
                await Move(int.Parse(args[0]), int.Parse(args[1]));
                return "ok";
            case "corner":
                for (var i = 0; i < 60; i++) { await Mouse(-127, -127, 0); await Task.Delay(8); }
                return "ok";
            case "click":
                _buttons |= Buttons[args.Length > 0 ? args[0] : "left"]; await Mouse(0, 0, 0);
                await Task.Delay(50);
                _buttons = 0; await Mouse(0, 0, 0);
                return "ok";
            case "down":
                _buttons |= Buttons[args.Length > 0 ? args[0] : "left"]; await Mouse(0, 0, 0);
                return "ok";
            case "up":
                _buttons = 0; await Mouse(0, 0, 0);
                return "ok";
            case "scroll":
            {
                var n = int.Parse(args[0]);
                for (var i = 0; i < Math.Abs(n); i++) { await Mouse(0, 0, Math.Sign(n)); await Task.Delay(20); }
                return "ok";
            }
            case "key":
            {
                var name = args[0].ToLowerInvariant();
                byte mods = 0;
                foreach (var m in args.Skip(1)) mods |= Mods[m.ToLowerInvariant()];
                byte code;
                if (Named.TryGetValue(name, out var n)) code = n;
                else if (args[0].Length == 1 && Chars.TryGetValue(args[0][0], out var c)) { code = c.Code; mods |= c.Mods; }
                else return $"err unknown key {args[0]}";
                await Key(code, mods);
                return "ok";
            }
            case "type":
                foreach (var ch in rest)
                    if (!Chars.ContainsKey(ch)) return $"err cannot type '{ch}'";
                foreach (var ch in rest) { var c = Chars[ch]; await Key(c.Code, c.Mods); }
                return "ok";
            default:
                return $"err unknown command '{cmd}'";
        }
    }

    static float F(string s) => float.Parse(s, CultureInfo.InvariantCulture);
    static int Clamp(int v) => Math.Clamp(v, -127, 127);

    static byte[] Absolute(float fx, float fy, byte buttons)
    {
        int x = (int)Math.Round(Math.Clamp(fx, 0, 1) * 32767), y = (int)Math.Round(Math.Clamp(fy, 0, 1) * 32767);
        return [(byte)(buttons & 7), (byte)(x & 0xFF), (byte)(x >> 8), (byte)(y & 0xFF), (byte)(y >> 8)];
    }

    Task Mouse(int dx, int dy, int wheel) =>
        hid.Send(2, [_buttons, (byte)(sbyte)dx, (byte)(sbyte)dy, (byte)(sbyte)wheel]);

    async Task Move(int dx, int dy)
    {
        while (dx != 0 || dy != 0)
        {
            var sx = Math.Clamp(dx, -8, 8); var sy = Math.Clamp(dy, -8, 8);
            await Mouse(sx, sy, 0);
            dx -= sx; dy -= sy;
            await Task.Delay(8);
        }
    }

    async Task Key(byte code, byte mods)
    {
        await hid.Send(1, [mods, 0, code, 0, 0, 0, 0, 0]);
        await Task.Delay(12);
        await hid.Send(1, new byte[8]);
        await Task.Delay(12);
    }

    async Task Consumer(string name, double hold)
    {
        await ConsumerSet(name, true);
        await Task.Delay(TimeSpan.FromSeconds(hold));
        await ConsumerSet(name, false);
    }

    Task ConsumerSet(string name, bool down)
    {
        var bit = (byte)(1 << ConsumerKeys[name]);
        _consumerBits = down ? (byte)(_consumerBits | bit) : (byte)(_consumerBits & ~bit);
        return hid.Send(4, [_consumerBits]);
    }
}

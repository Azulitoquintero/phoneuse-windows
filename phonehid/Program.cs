// PhoneHid: this PC as a Bluetooth keyboard + mouse for an iPhone, driven by
// phoneuse's line protocol on 127.0.0.1:8791 (replaces hid_daemon.py on Windows).
using System.Net;
using System.Net.Sockets;
using System.Text;
using PhoneHid;

const int Port = 8791;

using var hid = new BleHid();
hid.StatusChanged += s => Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] {s}");
await hid.StartAsync();
var daemon = new Daemon(hid);

var listener = new TcpListener(IPAddress.Loopback, Port);
listener.Start();
Console.WriteLine($"PhoneHid listo. En el iPhone: Ajustes > Bluetooth > \"{Environment.MachineName}\".");
Console.WriteLine($"Ordenes en 127.0.0.1:{Port}. Ctrl+C para salir.");

while (true)
{
    var client = await listener.AcceptTcpClientAsync();
    _ = Task.Run(() => Serve(client));
}

async Task Serve(TcpClient client)
{
    using (client)
    {
        try
        {
            var stream = client.GetStream();
            using var reader = new StreamReader(stream, new UTF8Encoding(false));
            using var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" };
            string? line;
            while ((line = await reader.ReadLineAsync()) is not null)
            {
                // A web page can reach a localhost port with an HTTP request; never run those.
                if (line.Contains("HTTP/") || line.StartsWith("Host:", StringComparison.OrdinalIgnoreCase)
                    || line.StartsWith("Origin:", StringComparison.OrdinalIgnoreCase))
                    return;
                if (line.Trim().Length == 0) continue;
                await writer.WriteLineAsync(await daemon.Run(line));
            }
        }
        catch (IOException) { }
    }
}

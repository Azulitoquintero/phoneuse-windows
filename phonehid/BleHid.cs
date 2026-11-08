// Based on BleKeyboard.cs from TypeOver (github.com/SaqibS/typeover). Serves the same
// HID report map as phoneuse's Linux daemon (github.com/lxe/phoneuse), over BLE:
//   1 keyboard, 2 relative mouse, 3 absolute pointer, 4 consumer keys.
using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.GenericAttributeProfile;
using static Windows.Devices.Bluetooth.GenericAttributeProfile.GattCharacteristicProperties;

namespace PhoneHid;

sealed class BleHid : IDisposable
{
    const ushort HidServiceId = 0x1812;
    const ushort HidInformationId = 0x2a4a;
    const ushort ReportMapId = 0x2a4b;
    const ushort ReportId = 0x2a4d;
    const ushort ControlPointId = 0x2a4c;
    const ushort ProtocolModeId = 0x2a4e;
    const ushort ReportReferenceId = 0x2908;
    const byte InputReport = 1;
    const byte OutputReport = 2;

    static readonly byte[] HidInformation = [0x11, 0x01, 0x00, 0x02];

    static readonly byte[] ReportMap = Convert.FromHexString(
        "05010906a1018501050719e029e71500250175019508810295017508810395067508150025650507190029658100c0" +
        "05010902a10185020901a1000509190129031500250195037501810295017505810305010930093109381581257f750895038106c0c0" +
        "05010902a10185030901a1000509190129031500250195037501810295017505810305010930093115002" +
        "6ff7f751095028102c0c0" +
        "050c0901a10185041500250175019501" +
        "09b88102" + "09cf8102" + "09408102" + "0a21028102" +
        "95048103c0");

    // Input report sizes (without the report ID).
    static readonly Dictionary<byte, int> InputSizes = new() { [1] = 8, [2] = 4, [3] = 5, [4] = 1 };

    GattServiceProvider? _provider;
    readonly Dictionary<byte, GattLocalCharacteristic> _inputs = [];

    public event Action<string>? StatusChanged;
    public bool Connected => _inputs.Values.Any(c => c.SubscribedClients.Count > 0);
    public string Peer => _inputs.Values.SelectMany(c => c.SubscribedClients)
        .Select(c => c.Session.DeviceId.Id).FirstOrDefault() ?? "None";

    public async Task StartAsync()
    {
        var result = await GattServiceProvider.CreateAsync(Uuid(HidServiceId));
        Check(result.Error, "Create HID service");
        _provider = result.ServiceProvider;

        await CharacteristicAsync(HidInformationId, Read, HidInformation);
        await CharacteristicAsync(ReportMapId, Read, ReportMap);

        var mode = await CharacteristicAsync(ProtocolModeId, Read | WriteWithoutResponse);
        AnswerReads(mode, [1]);
        mode.WriteRequested += AcceptWrite;

        foreach (var (id, size) in InputSizes)
        {
            var c = await CharacteristicAsync(ReportId, Read | Notify);
            AnswerReads(c, new byte[size]);
            await ReportReferenceAsync(c, id, InputReport);
            c.SubscribedClientsChanged += (_, _) => StatusChanged?.Invoke(
                Connected ? $"iPhone conectado ({_inputs.Count(x => x.Value.SubscribedClients.Count > 0)}/4 informes)"
                          : "iPhone desconectado");
            _inputs[id] = c;
        }

        var leds = await CharacteristicAsync(ReportId, Read | Write | WriteWithoutResponse);
        AnswerReads(leds, new byte[1]);
        leds.WriteRequested += AcceptWrite;
        await ReportReferenceAsync(leds, 1, OutputReport);

        var control = await CharacteristicAsync(ControlPointId, WriteWithoutResponse);
        control.WriteRequested += AcceptWrite;

        _provider.AdvertisementStatusChanged += (_, args) =>
            StatusChanged?.Invoke($"Bluetooth advertising: {args.Status}");
        _provider.StartAdvertising(new GattServiceProviderAdvertisingParameters
        {
            IsConnectable = true,
            IsDiscoverable = true
        });
    }

    /// Sends one input report (payload without the report ID).
    public async Task Send(byte reportId, byte[] payload)
    {
        if (!_inputs.TryGetValue(reportId, out var c))
            throw new ArgumentException($"unknown report {reportId}");
        if (c.SubscribedClients.Count == 0)
            throw new InvalidOperationException("no iPhone connected");
        await c.NotifyValueAsync(payload.AsBuffer());
    }

    async Task<GattLocalCharacteristic> CharacteristicAsync(
        ushort uuid, GattCharacteristicProperties properties, byte[]? staticValue = null)
    {
        var result = await _provider!.Service.CreateCharacteristicAsync(Uuid(uuid),
            new GattLocalCharacteristicParameters
            {
                CharacteristicProperties = properties,
                ReadProtectionLevel = GattProtectionLevel.EncryptionRequired,
                WriteProtectionLevel = GattProtectionLevel.EncryptionRequired,
                StaticValue = staticValue?.AsBuffer()
            });
        Check(result.Error, $"Create characteristic {uuid:X4}");
        return result.Characteristic;
    }

    static async Task ReportReferenceAsync(GattLocalCharacteristic c, byte id, byte type)
    {
        var result = await c.CreateDescriptorAsync(Uuid(ReportReferenceId),
            new GattLocalDescriptorParameters
            {
                ReadProtectionLevel = GattProtectionLevel.EncryptionRequired,
                StaticValue = new byte[] { id, type }.AsBuffer()
            });
        Check(result.Error, "Create report reference");
    }

    static void AnswerReads(GattLocalCharacteristic c, byte[] value) =>
        c.ReadRequested += async (_, args) =>
        {
            using var deferral = args.GetDeferral();
            var request = await args.GetRequestAsync();
            request.RespondWithValue(value.AsBuffer());
        };

    static async void AcceptWrite(GattLocalCharacteristic sender, GattWriteRequestedEventArgs args)
    {
        using var deferral = args.GetDeferral();
        var request = await args.GetRequestAsync();
        if (request.Option == GattWriteOption.WriteWithResponse)
            request.Respond();
    }

    static Guid Uuid(ushort value) => new($"{value:x8}-0000-1000-8000-00805f9b34fb");

    static void Check(BluetoothError error, string operation)
    {
        if (error != BluetoothError.Success)
            throw new InvalidOperationException($"{operation}: {error}");
    }

    public void Dispose() => _provider?.StopAdvertising();
}

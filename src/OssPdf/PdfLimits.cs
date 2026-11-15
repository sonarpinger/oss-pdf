namespace OssPdf;

public sealed class PdfLimits
{
    public const long RequestBytes = 60_000_000;
    public string ScratchRoot { get; init; } = Path.GetTempPath();
    public int QueueWaitMilliseconds { get; init; } = 500;
    public int TimeoutSeconds { get; init; } = 120;
    public int HtmlConcurrency { get; init; } = 2;
    public int WordConcurrency { get; init; } = 2;
    public int PdfConcurrency { get; init; } = 2;
    public int RasterConcurrency { get; init; } = 1;
    public int MaxPages { get; init; } = 200;
    public int MaxMergeFiles { get; init; } = 32;
    public long MaxImagePixels { get; init; } = 25_000_000;
    public long MaxRasterPixels { get; init; } = 200_000_000;
    public long MaxScratchBytes { get; init; } = 512 * 1024 * 1024;
    public long MaxOutputBytes { get; init; } = 128 * 1024 * 1024;
    public bool ChromiumSandbox { get; init; } = true;

    public bool IsValid() =>
        !string.IsNullOrWhiteSpace(ScratchRoot) && QueueWaitMilliseconds >= 0 && TimeoutSeconds > 0 &&
        HtmlConcurrency > 0 && WordConcurrency > 0 && PdfConcurrency > 0 && RasterConcurrency > 0 &&
        MaxPages > 0 && MaxMergeFiles >= 2 && MaxImagePixels > 0 && MaxRasterPixels >= MaxImagePixels &&
        MaxOutputBytes > 0 && MaxScratchBytes > MaxOutputBytes;
}

internal sealed class PdfRequestException(int statusCode, string message) : Exception(message)
{
    public int StatusCode { get; } = statusCode;
}

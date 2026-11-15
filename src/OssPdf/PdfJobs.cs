using System.Diagnostics;
using Microsoft.Extensions.Options;

namespace OssPdf;

public enum PdfEngine { Html, Word, Pdf, Raster }

public sealed class PdfJobs(IOptions<PdfLimits> options, ILogger<PdfJobs> logger) : IDisposable
{
    private readonly PdfLimits _limits = options.Value;
    private readonly Dictionary<PdfEngine, SemaphoreSlim> _slots = new()
    {
        [PdfEngine.Html] = new(options.Value.HtmlConcurrency),
        [PdfEngine.Word] = new(options.Value.WordConcurrency),
        [PdfEngine.Pdf] = new(options.Value.PdfConcurrency),
        [PdfEngine.Raster] = new(options.Value.RasterConcurrency)
    };

    public async Task<IResult> RunAsync(
        HttpContext context,
        PdfEngine engine,
        Func<PdfJob, CancellationToken, Task<string>> convert)
    {
        var timer = Stopwatch.StartNew();
        var slots = _slots[engine];
        var abort = context.RequestAborted;
        if (!await slots.WaitAsync(_limits.QueueWaitMilliseconds, abort))
        {
            context.Response.Headers.RetryAfter = "1";
            throw new PdfRequestException(503, "The converter is busy. Try again shortly.");
        }

        var wait = timer.ElapsedMilliseconds;
        PdfJob? job = null;
        var responseOwnsSlot = false;
        try
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(_limits.TimeoutSeconds));
            using var storageLimit = new CancellationTokenSource();
            using var combined = CancellationTokenSource.CreateLinkedTokenSource(
                abort, timeout.Token, storageLimit.Token);
            using var monitorStop = new CancellationTokenSource();

            job = new PdfJob(_limits.ScratchRoot, logger);
            var monitor = MonitorStorageAsync(job, storageLimit, monitorStop.Token);
            try
            {
                var name = await convert(job, combined.Token);
                combined.Token.ThrowIfCancellationRequested();
                var output = new FileInfo(job.OutputPath);
                if (!output.Exists || output.Length == 0)
                {
                    throw new PdfRequestException(422, "Conversion produced no PDF.");
                }

                if (output.Length > _limits.MaxOutputBytes || job.ExceedsStorage(_limits.MaxScratchBytes))
                {
                    throw StorageLimitExceeded();
                }

                await Uploads.CheckSignatureAsync(output.FullName, UploadKind.Pdf, combined.Token);
                var result = Results.File(output.FullName, "application/pdf", name);
                var responseJob = job;
                context.Response.OnCompleted(() =>
                {
                    responseJob.Dispose();
                    slots.Release();
                    return Task.CompletedTask;
                });
                responseOwnsSlot = true;
                job = null;
                return result;
            }
            catch (OperationCanceledException) when (!abort.IsCancellationRequested && storageLimit.IsCancellationRequested)
            {
                throw StorageLimitExceeded();
            }
            catch (OperationCanceledException) when (!abort.IsCancellationRequested && timeout.IsCancellationRequested)
            {
                throw new PdfRequestException(504, "The conversion timed out.");
            }
            finally
            {
                await monitorStop.CancelAsync();
                await monitor;
            }
        }
        finally
        {
            job?.Dispose();
            if (!responseOwnsSlot)
            {
                slots.Release();
            }

            logger.LogInformation("{Endpoint} queue wait {QueueMs} ms, processing {ProcessingMs} ms",
                context.Request.Path, wait, timer.ElapsedMilliseconds - wait);
        }
    }

    private async Task MonitorStorageAsync(PdfJob job, CancellationTokenSource limit, CancellationToken stop)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(250));
        try
        {
            while (await timer.WaitForNextTickAsync(stop))
            {
                if (job.ExceedsStorage(_limits.MaxScratchBytes) ||
                    new FileInfo(job.OutputPath) is { Exists: true } output && output.Length > _limits.MaxOutputBytes)
                {
                    await limit.CancelAsync();
                    break;
                }
            }
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested)
        {
        }
    }

    private static PdfRequestException StorageLimitExceeded() =>
        new(422, "The document exceeds the conversion storage limit.");

    public void Dispose()
    {
        foreach (var slots in _slots.Values)
        {
            slots.Dispose();
        }
    }
}

public sealed class PdfJob : IDisposable
{
    private readonly ILogger _logger;

    internal PdfJob(string root, ILogger logger)
    {
        _logger = logger;
        Directory.CreateDirectory(root);
        DirectoryPath = Path.GetFullPath(Path.Combine(root, $"oss-pdf-{Guid.NewGuid():N}"));
        if (OperatingSystem.IsWindows())
        {
            Directory.CreateDirectory(DirectoryPath);
        }
        else
        {
            Directory.CreateDirectory(DirectoryPath, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
    }

    public string DirectoryPath { get; }
    public string OutputPath => PathFor("output.pdf");
    public string PathFor(string name) => Path.Combine(DirectoryPath, name);

    internal bool ExceedsStorage(long maxBytes)
    {
        long bytes = 0;
        foreach (var file in new DirectoryInfo(DirectoryPath).EnumerateFiles("*", new EnumerationOptions
        {
            RecurseSubdirectories = true,
            AttributesToSkip = FileAttributes.ReparsePoint,
            IgnoreInaccessible = true
        }))
        {
            try
            {
                bytes += file.Length;
                if (bytes > maxBytes)
                {
                    return true;
                }
            }
            catch (FileNotFoundException)
            {
                // A converter removed an intermediate file during enumeration.
            }
        }

        return false;
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(DirectoryPath, recursive: true);
        }
        catch (IOException exception)
        {
            _logger.LogWarning(exception, "Could not remove a conversion workspace.");
        }
        catch (UnauthorizedAccessException exception)
        {
            _logger.LogWarning(exception, "Could not remove a conversion workspace.");
        }
    }
}

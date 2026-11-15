using System.Diagnostics;
using System.Text;

namespace OssPdf;

internal static class NativeTool
{
    public static async Task<string> RunAsync(
        PdfJob job, string executable, IEnumerable<string> arguments, CancellationToken cancellationToken)
    {
        var start = new ProcessStartInfo(executable)
        {
            WorkingDirectory = job.DirectoryPath,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        start.Environment["LC_ALL"] = "C";
        start.Environment["TMPDIR"] = job.DirectoryPath;
        start.Environment["MAGICK_TEMPORARY_PATH"] = job.DirectoryPath;
        start.Environment.Remove("GS_OPTIONS");
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }

        cancellationToken.ThrowIfCancellationRequested();
        using var process = Process.Start(start) ?? throw new InvalidOperationException($"Could not start {executable}.");
        var stdout = DrainAsync(process.StandardOutput, capture: true, cancellationToken);
        var stderr = DrainAsync(process.StandardError, capture: false, cancellationToken);
        try
        {
            await process.WaitForExitAsync(cancellationToken);
            await Task.WhenAll(stdout, stderr);
        }
        catch (OperationCanceledException)
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                    await process.WaitForExitAsync();
                }
            }
            catch (InvalidOperationException) when (process.HasExited)
            {
            }

            throw;
        }

        if (process.ExitCode != 0)
        {
            throw new PdfRequestException(422, $"The document could not be processed by {executable}.");
        }

        return await stdout ?? throw new PdfRequestException(422, $"The document metadata exceeded the {executable} limit.");
    }

    private static async Task<string?> DrainAsync(StreamReader reader, bool capture, CancellationToken cancellationToken)
    {
        const int maxCapturedChars = 128 * 1024;
        var captured = new StringBuilder();
        var buffer = new char[4096];
        var truncated = false;
        while (await reader.ReadAsync(buffer.AsMemory(), cancellationToken) is var count && count > 0)
        {
            if (capture)
            {
                var remaining = maxCapturedChars - captured.Length;
                truncated |= count > remaining;
                captured.Append(buffer, 0, Math.Min(count, remaining));
            }
        }

        return truncated ? null : captured.ToString();
    }
}

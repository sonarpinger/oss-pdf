using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using HtmlAgilityPack;
using Ironpress;
using PdfSharp.Drawing;
using PdfSharp.Pdf;
using PdfSharp.Pdf.IO;

namespace OssPdf;

public sealed class IronpressWorker : IAsyncDisposable
{
    private readonly ConcurrentBag<Process> _idle = [];

    internal async Task RenderAsync(HtmlToPdfRequest request, HtmlPageMargins margins, PdfJob job,
        int maxPages, CancellationToken cancellationToken)
    {
        var input = new RenderJob(job.DirectoryPath, margins, request.Header, request.Footer, maxPages);
        await File.WriteAllTextAsync(job.PathFor("body.html"), request.Body, cancellationToken);
        var worker = await RentAsync();
        var reuse = false;
        try
        {
            await worker.StandardInput.WriteLineAsync(JsonSerializer.Serialize(input).AsMemory(), cancellationToken);
            await worker.StandardInput.FlushAsync(cancellationToken);
            var outcome = await worker.StandardOutput.ReadLineAsync(cancellationToken);
            cancellationToken.ThrowIfCancellationRequested();
            reuse = outcome is "ok" or "too-many-pages" or "invalid";
            switch (outcome)
            {
                case "ok":
                    break;
                case "too-many-pages":
                    throw new PdfRequestException(422, "The PDF exceeds the page limit.");
                default:
                    throw RenderFailed();
            }
        }
        catch (IOException)
        {
            cancellationToken.ThrowIfCancellationRequested();
            throw RenderFailed();
        }
        finally
        {
            if (reuse)
            {
                _idle.Add(worker);
            }
            else
            {
                await StopAsync(worker);
            }
        }

        if (File.Exists(job.PathFor("numbered-margins.pdf")))
        {
            await NativeTool.RunAsync(job, "qpdf",
                ["body.pdf", "--overlay", "numbered-margins.pdf", "--", job.OutputPath], cancellationToken);
        }
    }

    private async Task<Process> RentAsync()
    {
        while (_idle.TryTake(out var worker))
        {
            if (!worker.HasExited)
            {
                return worker;
            }

            await StopAsync(worker);
        }

        var executable = Environment.ProcessPath ?? throw new InvalidOperationException("Application path is unavailable.");
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true
        };
        if (string.Equals(Path.GetFileNameWithoutExtension(executable), "dotnet", StringComparison.OrdinalIgnoreCase))
        {
            start.ArgumentList.Add(typeof(IronpressWorker).Assembly.Location);
        }

        start.ArgumentList.Add("--ironpress-worker");
        return Process.Start(start) ?? throw new InvalidOperationException("Could not start the Ironpress worker.");
    }

    private static async Task StopAsync(Process worker)
    {
        using (worker)
        {
            try
            {
                if (!worker.HasExited)
                {
                    worker.Kill();
                }
            }
            catch (InvalidOperationException) when (worker.HasExited)
            {
            }

            await worker.WaitForExitAsync();
        }
    }

    public async ValueTask DisposeAsync()
    {
        while (_idle.TryTake(out var worker))
        {
            await StopAsync(worker);
        }
    }

    internal static void Run()
    {
        while (Console.ReadLine() is { } line)
        {
            var job = JsonSerializer.Deserialize<RenderJob>(line)!;
            try
            {
                Console.WriteLine(Render(job));
            }
            catch (Exception exception) when (exception is IronpressException or PdfReaderException)
            {
                Console.WriteLine("invalid");
            }
        }
    }

    private static string Render(RenderJob job)
    {
        var input = job.PathFor("body.html");
        var body = File.ReadAllText(input);
        File.Delete(input);

        var header = FindPageCounters(job.Header);
        var footer = FindPageCounters(job.Footer);
        var numbered = header.Length > 0 || footer.Length > 0;
        var margins = PageMargins.FromPoints(ToPoints(job.Margins.Top), ToPoints(job.Margins.Right),
            ToPoints(job.Margins.Bottom), ToPoints(job.Margins.Left));
        using var converter = new HtmlConverter().SetPageSize(PageSize.Letter).SetMargins(margins);
        SetFragments(converter, header.Length == 0 ? job.Header : null, footer.Length == 0 ? job.Footer : null);
        var bodyPath = job.PathFor(numbered ? "body.pdf" : "output.pdf");
        File.WriteAllBytes(bodyPath, converter.ConvertHtml(body));
        if (!numbered)
        {
            return "ok";
        }

        // Ironpress repeats rich margin fragments verbatim. Resolve the numbered fragments
        // after pagination and overlay only those margin bands on the original pages.
        using var original = PdfReader.Open(bodyPath, PdfDocumentOpenMode.Import);
        if (original.PageCount > job.MaxPages)
        {
            return "too-many-pages";
        }

        using var overlay = new PdfDocument();
        using var fragments = new HtmlConverter().SetMargins(margins);
        for (var index = 0; index < original.PageCount; index++)
        {
            SetFragments(fragments, NumberedFragment(header, index + 1, original.PageCount),
                NumberedFragment(footer, index + 1, original.PageCount));
            var page = overlay.AddPage();
            page.Width = original.Pages[index].Width;
            page.Height = original.Pages[index].Height;
            fragments.SetCustomPageSize(PageDimensions.FromPoints((float)page.Width.Point, (float)page.Height.Point));
            using var pdf = new MemoryStream(fragments.ConvertHtml("<body></body>"));
            using var form = XPdfForm.FromStream(pdf);
            using var graphics = XGraphics.FromPdfPage(page);
            var bands = new XGraphicsPath();
            if (header.Length > 0)
            {
                bands.AddRectangle(0, 0, page.Width.Point, margins.Top);
            }

            if (footer.Length > 0)
            {
                bands.AddRectangle(0, page.Height.Point - margins.Bottom, page.Width.Point, margins.Bottom);
            }

            graphics.IntersectClip(bands);
            graphics.DrawImage(form, 0, 0, page.Width.Point, page.Height.Point);
        }

        overlay.Save(job.PathFor("numbered-margins.pdf"));
        return "ok";
    }

    private static void SetFragments(HtmlConverter converter, string? header, string? footer)
    {
        if (header is not null)
        {
            converter.SetHeaderHtml(header);
        }

        if (footer is not null)
        {
            converter.SetFooterHtml(footer);
        }
    }

    private static HtmlNode[] FindPageCounters(string? fragment)
    {
        if (fragment is null)
        {
            return [];
        }

        var document = new HtmlDocument();
        document.LoadHtml(fragment);
        return document.DocumentNode.Descendants()
            .Where(node => node.HasClass("pageNumber") || node.HasClass("totalPages"))
            .ToArray();
    }

    private static string? NumberedFragment(HtmlNode[] counters, int page, int total)
    {
        if (counters.Length == 0)
        {
            return null;
        }

        foreach (var node in counters)
        {
            node.InnerHtml = (node.HasClass("totalPages") ? total : page).ToString(CultureInfo.InvariantCulture);
        }

        return counters[0].OwnerDocument.DocumentNode.OuterHtml;
    }

    private static float ToPoints(double millimeters) => (float)(millimeters * 72 / 25.4);
    private static PdfRequestException RenderFailed() => new(422, "Ironpress could not render the HTML document.");

    private sealed record RenderJob(string Directory, HtmlPageMargins Margins, string? Header, string? Footer, int MaxPages)
    {
        public string PathFor(string name) => Path.Combine(Directory, name);
    }
}

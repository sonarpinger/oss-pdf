using System.Globalization;
using System.Text.Json;
using Microsoft.Extensions.Options;
using PdfSharp.Drawing;
using PdfSharp.Pdf;

namespace OssPdf;

public sealed class PdfConverters(IOptions<PdfLimits> options)
{
    private const int RasterDpi = 150;
    private PdfLimits Limits => options.Value;

    public async Task WordToPdfAsync(PdfJob job, string input, CancellationToken cancellationToken)
    {
        Uploads.CheckWordPackage(input, cancellationToken);
        var profile = job.PathFor("office-profile");
        var settings = Path.Combine(profile, "user", "registrymodifications.xcu");
        Directory.CreateDirectory(Path.GetDirectoryName(settings)!);
        await File.WriteAllTextAsync(settings, """
            <?xml version="1.0" encoding="UTF-8"?>
            <oor:items xmlns:oor="http://openoffice.org/2001/registry">
              <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
                <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
              </item>
            </oor:items>
            """, cancellationToken);
        await NativeTool.RunAsync(job, "soffice",
            ["-env:UserInstallation=" + new Uri(profile).AbsoluteUri, "--headless", "--norestore",
                "--convert-to", "pdf:writer_pdf_Export", "--outdir", job.DirectoryPath, input], cancellationToken);
        var converted = job.PathFor(Path.GetFileNameWithoutExtension(input) + ".pdf");
        if (!File.Exists(converted))
        {
            throw new PdfRequestException(422, "LibreOffice could not convert the DOCX.");
        }

        File.Move(converted, job.OutputPath);
    }

    public async Task CompressAsync(PdfJob job, string input, int quality, CancellationToken cancellationToken)
    {
        await InspectPdfAsync(job, input, cancellationToken);
        await NativeTool.RunAsync(job, "qpdf",
            ["--object-streams=generate", "--recompress-flate", "--compression-level=9", "--optimize-images",
                "--jpeg-quality=" + quality.ToString(CultureInfo.InvariantCulture), input, job.OutputPath], cancellationToken);
    }

    public async Task RasterizeAsync(PdfJob job, string input, int quality, CancellationToken cancellationToken)
    {
        var dimensions = await NativeTool.RunAsync(job, "pdfium-render",
            [input, RasterDpi.ToString(CultureInfo.InvariantCulture), Limits.MaxPages.ToString(CultureInfo.InvariantCulture),
                Limits.MaxImagePixels.ToString(CultureInfo.InvariantCulture),
                Limits.MaxRasterPixels.ToString(CultureInfo.InvariantCulture)], cancellationToken);
        var pages = dimensions.Split('\n', StringSplitOptions.RemoveEmptyEntries);
        var rendered = Directory.GetFiles(job.DirectoryPath, "page-*.png").Order(StringComparer.Ordinal).ToArray();
        if (rendered.Length == 0 || rendered.Length != pages.Length)
        {
            throw new PdfRequestException(422, "PDFium could not render every page.");
        }

        using var document = new PdfDocument();
        for (var index = 0; index < rendered.Length; index++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var png = rendered[index];
            var jpeg = Path.ChangeExtension(png, ".jpg");
            await NativeTool.RunAsync(job, "convert",
                ["PNG:" + png, "-strip", "-quality", quality.ToString(CultureInfo.InvariantCulture), "JPEG:" + jpeg],
                cancellationToken);
            File.Delete(png);
            var pageSize = ReadPageSize(pages[index]);
            var page = document.AddPage();
            page.Width = XUnit.FromPoint(pageSize.Width);
            page.Height = XUnit.FromPoint(pageSize.Height);
            using var image = XImage.FromFile(jpeg);
            using var graphics = XGraphics.FromPdfPage(page);
            graphics.DrawImage(image, 0, 0, pageSize.Width, pageSize.Height);
        }

        await document.SaveAsync(job.OutputPath);
    }

    public async Task MergeAsync(PdfJob job, IReadOnlyList<string> inputs, CancellationToken cancellationToken)
    {
        var remainingPages = Limits.MaxPages;
        foreach (var input in inputs)
        {
            var count = await InspectPdfAsync(job, input, cancellationToken);
            remainingPages -= count;
            if (remainingPages < 0)
            {
                throw TooManyPages();
            }
        }

        await NativeTool.RunAsync(job, "qpdf", ["--empty", "--pages", .. inputs, "--", job.OutputPath], cancellationToken);
    }

    public async Task ImageToPdfAsync(PdfJob job, string input, CancellationToken cancellationToken)
    {
        var coder = Path.GetExtension(input) == ".png" ? "PNG:" : "JPEG:";
        var dimensions = await NativeTool.RunAsync(job, "identify",
            ["-ping", "-format", "%w %h", coder + input], cancellationToken);
        var sides = dimensions.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (sides.Length != 2 || !int.TryParse(sides[0], CultureInfo.InvariantCulture, out var width) ||
            !int.TryParse(sides[1], CultureInfo.InvariantCulture, out var height) ||
            width <= 0 || height <= 0 || (long)width * height > Limits.MaxImagePixels)
        {
            throw new PdfRequestException(422, "The image exceeds the pixel limit or has invalid dimensions.");
        }

        await NativeTool.RunAsync(job, "convert",
            [coder + input, "-auto-orient", "-background", "white", "-alpha", "remove", "-alpha", "off",
                "-units", "PixelsPerInch", "-density", RasterDpi.ToString(CultureInfo.InvariantCulture),
                "PDF:" + job.OutputPath], cancellationToken);
    }

    public Task CheckGeneratedPdfAsync(PdfJob job, CancellationToken cancellationToken) =>
        InspectPdfAsync(job, job.OutputPath, cancellationToken);

    private async Task<int> InspectPdfAsync(PdfJob job, string path, CancellationToken cancellationToken)
    {
        var countText = await NativeTool.RunAsync(job, "qpdf", ["--show-npages", path], cancellationToken);
        if (!int.TryParse(countText, CultureInfo.InvariantCulture, out var count) || count <= 0)
        {
            throw new PdfRequestException(422, "The PDF has an invalid page count.");
        }

        if (count > Limits.MaxPages)
        {
            throw TooManyPages();
        }

        var security = await NativeTool.RunAsync(job, "qpdf", ["--json=2", "--json-key=encrypt", path], cancellationToken);
        using var metadata = JsonDocument.Parse(security);
        if (metadata.RootElement.GetProperty("encrypt").GetProperty("encrypted").GetBoolean())
        {
            throw new PdfRequestException(422, "The PDF is encrypted.");
        }

        return count;
    }

    private static PageSize ReadPageSize(string dimensions)
    {
        var sides = dimensions.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (sides.Length != 2 || !double.TryParse(sides[0], CultureInfo.InvariantCulture, out var width) ||
            !double.TryParse(sides[1], CultureInfo.InvariantCulture, out var height) ||
            !double.IsFinite(width) || !double.IsFinite(height) || width <= 0 || height <= 0)
        {
            throw new PdfRequestException(422, "Could not determine the PDF page dimensions.");
        }

        return new(width, height);
    }

    private sealed record PageSize(double Width, double Height);

    private static PdfRequestException TooManyPages() => new(422, "The PDF page limit was exceeded.");
}

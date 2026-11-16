using System.Globalization;
using System.Text.Json;
using Microsoft.Extensions.Options;
using PdfSharp.Drawing;
using PdfSharp.Pdf;
using SkiaSharp;

namespace OssPdf;

public sealed class PdfConverters(IOptions<PdfLimits> options, IronpressWorker ironpress)
{
    private const int ImageDpi = 150;
    private const int SmallImageMaxSide = 1600;
    private const int SmallImageMinSide = 640;
    private const int EstimatedSmallPdfOverhead = 2_000;
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

    public async Task ImageToPdfAsync(PdfJob job, string input, long? targetBytes, CancellationToken cancellationToken)
    {
        var small = targetBytes.HasValue;
        var coder = Path.GetExtension(input) == ".png" ? "PNG:" : "JPEG:";
        var dimensions = await NativeTool.RunAsync(job, "identify",
            ["-ping", "-format", small ? "%w %h %[orientation]" : "%w %h", coder + input], cancellationToken);
        var sides = dimensions.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (sides.Length != (small ? 3 : 2) || !int.TryParse(sides[0], CultureInfo.InvariantCulture, out var width) ||
            !int.TryParse(sides[1], CultureInfo.InvariantCulture, out var height) ||
            width <= 0 || height <= 0 || (long)width * height > Limits.MaxImagePixels)
        {
            throw new PdfRequestException(422, "The image exceeds the pixel limit or has invalid dimensions.");
        }

        string[] resize = small ? ["-resize", FormattableString.Invariant($"{SmallImageMaxSide}x{SmallImageMaxSide}>")] : [];
        var normalized = job.PathFor(IronpressWorker.ImageFile);
        await NativeTool.RunAsync(job, "convert",
            [coder + input, "-auto-orient", "-background", "white", "-alpha", "remove", "-alpha", "off",
                "-colorspace", "sRGB", .. resize, "-strip", "-interlace", "none", "-depth", "8",
                "-define", "png:color-type=2", "-define", "png:compression-level=1",
                "PNG:" + normalized], cancellationToken);
        if (targetBytes is { } target)
        {
            if (sides[2] is "LeftTop" or "RightTop" or "RightBottom" or "LeftBottom")
            {
                (width, height) = (height, width);
            }

            var pageSize = new PageSize(width * 72d / ImageDpi, height * 72d / ImageDpi);
            using var bitmap = SKBitmap.Decode(normalized) ??
                throw new PdfRequestException(422, "Could not decode the normalized image.");
            await SaveSmallImagePdfAsync(job.OutputPath, bitmap, pageSize, target, cancellationToken);
            return;
        }

        await ironpress.RenderImageAsync(job, ImageDpi, cancellationToken);
    }

    private static async Task SaveSmallImagePdfAsync(
        string path, SKBitmap image, PageSize pageSize, long targetBytes, CancellationToken cancellationToken)
    {
        var jpegBudget = Math.Max(1, targetBytes - EstimatedSmallPdfOverhead);
        var originalSide = Math.Max(image.Width, image.Height);
        var minimumSide = Math.Min(originalSide, SmallImageMinSide);
        var side = originalSide;
        for (var attempt = 0; attempt < 3; attempt++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            using var jpeg = EncodeJpeg(image, originalSide, side, 76);
            if (jpeg.Size <= jpegBudget)
            {
                var pdfBytes = await SaveImagePdfAsync(path, jpeg, pageSize);
                if (pdfBytes <= targetBytes)
                {
                    return;
                }

                jpegBudget = Math.Max(1, targetBytes - (pdfBytes - jpeg.Size));
            }

            if (side == minimumSide)
            {
                break;
            }

            var scale = Math.Min(0.94, 0.96 * Math.Sqrt((double)jpegBudget / jpeg.Size));
            side = Math.Max(minimumSide, (int)(side * scale));
        }

        cancellationToken.ThrowIfCancellationRequested();
        using var fallback = EncodeJpeg(image, originalSide, minimumSide, 50);
        await SaveImagePdfAsync(path, fallback, pageSize);
    }

    private static async Task<long> SaveImagePdfAsync(string path, SKData jpeg, PageSize pageSize)
    {
        var bytes = jpeg.ToArray();
        using var input = new MemoryStream(bytes, 0, bytes.Length, writable: false, publiclyVisible: true);
        using var document = new PdfDocument();
        using var image = XImage.FromStream(input);
        AddImagePage(document, image, pageSize);
        await document.SaveAsync(path);
        return new FileInfo(path).Length;
    }

    private static SKData EncodeJpeg(SKBitmap image, int originalSide, int side, int quality)
    {
        var scale = (double)side / originalSide;
        var size = new SKSizeI(Math.Max(1, (int)Math.Round(image.Width * scale)),
            Math.Max(1, (int)Math.Round(image.Height * scale)));
        using var resized = side == originalSide ? null
            : image.Resize(size, new SKSamplingOptions(SKCubicResampler.Mitchell)) ??
                throw new PdfRequestException(422, "Could not resize the image.");
        return (resized ?? image).Encode(SKEncodedImageFormat.Jpeg, quality) ??
            throw new PdfRequestException(422, "Could not encode the image as JPEG.");
    }

    private static void AddImagePage(PdfDocument document, XImage image, PageSize size)
    {
        var page = document.AddPage();
        page.Width = XUnit.FromPoint(size.Width);
        page.Height = XUnit.FromPoint(size.Height);
        using var graphics = XGraphics.FromPdfPage(page);
        graphics.DrawImage(image, 0, 0, size.Width, size.Height);
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

    private sealed record PageSize(double Width, double Height);

    private static PdfRequestException TooManyPages() => new(422, "The PDF page limit was exceeded.");
}

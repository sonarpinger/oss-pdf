using System.Globalization;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Options;
using PdfSharp.Drawing;
using PdfSharp.Pdf;

namespace OssPdf;

public sealed partial class PdfConverters(IOptions<PdfLimits> options)
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
        await InspectPdfAsync(job, input, geometry: false, cancellationToken);
        var dpi = (96 + (quality - 1) * 204 / 99).ToString(CultureInfo.InvariantCulture);
        var factor = (1 - (quality - 1) * 0.85 / 99).ToString("0.000", CultureInfo.InvariantCulture);
        await NativeTool.RunAsync(job, "gs",
            ["-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
                "-dAutoRotatePages=/None", "-dDetectDuplicateImages=true",
                "-dDownsampleColorImages=true", "-dColorImageResolution=" + dpi,
                "-dDownsampleGrayImages=true", "-dGrayImageResolution=" + dpi,
                "-dAutoFilterColorImages=false", "-dColorImageFilter=/DCTEncode",
                "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/DCTEncode",
                "-sOutputFile=" + job.OutputPath, "-c",
                $"<< /ColorImageDict << /QFactor {factor} /Blend 1 >> /GrayImageDict << /QFactor {factor} /Blend 1 >> >> setdistillerparams",
                "-f", input], cancellationToken);
    }

    public async Task RasterizeAsync(PdfJob job, string input, int quality, CancellationToken cancellationToken)
    {
        var (_, pages) = await InspectPdfAsync(job, input, geometry: true, cancellationToken);
        double totalPixels = 0;
        foreach (var page in pages)
        {
            var pixels = Math.Ceiling(page.Width * RasterDpi / 72) * Math.Ceiling(page.Height * RasterDpi / 72);
            totalPixels += pixels;
            if (page.Width <= 0 || page.Height <= 0 || pixels > Limits.MaxImagePixels || totalPixels > Limits.MaxRasterPixels)
            {
                throw new PdfRequestException(422, "The PDF exceeds the raster pixel limit.");
            }
        }

        await NativeTool.RunAsync(job, "gs",
            ["-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dUseCropBox", "-dNumRenderingThreads=2",
                "-sDEVICE=png16m", $"-r{RasterDpi}", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
                "-sOutputFile=" + job.PathFor("page-%06d.png"), input], cancellationToken);

        var rendered = Directory.GetFiles(job.DirectoryPath, "page-*.png").Order(StringComparer.Ordinal).ToArray();
        if (rendered.Length != pages.Length)
        {
            throw new PdfRequestException(422, "Ghostscript could not render every page.");
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
            var pageSize = pages[index];
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
            var (count, _) = await InspectPdfAsync(job, input, geometry: false, cancellationToken);
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

    public async Task CheckGeneratedPdfAsync(PdfJob job, CancellationToken cancellationToken)
    {
        await InspectPdfAsync(job, job.OutputPath, geometry: false, cancellationToken);
    }

    private async Task<(int Count, PageSize[] Sizes)> InspectPdfAsync(
        PdfJob job, string path, bool geometry, CancellationToken cancellationToken)
    {
        string[] arguments = geometry
            ? ["-f", "1", "-l", Limits.MaxPages.ToString(CultureInfo.InvariantCulture), path]
            : [path];
        var info = await NativeTool.RunAsync(job, "pdfinfo", arguments, cancellationToken);
        var countField = PageCount().Matches(info).LastOrDefault();
        if (countField is null || !int.TryParse(countField.Groups[1].ValueSpan, CultureInfo.InvariantCulture, out var count) ||
            count <= 0 || Encrypted().Match(info, countField.Index).Success)
        {
            throw new PdfRequestException(422, "The PDF is invalid or password-protected.");
        }

        if (count > Limits.MaxPages)
        {
            throw TooManyPages();
        }

        if (!geometry)
        {
            return (count, []);
        }

        var sizes = PageDimensions().Matches(info, countField.Index);
        var rotations = PageRotation().Matches(info, countField.Index);
        if (sizes.Count != count || rotations.Count != count)
        {
            throw new PdfRequestException(422, "Could not determine the PDF page dimensions.");
        }

        var pages = new PageSize[count];
        for (var index = 0; index < count; index++)
        {
            var width = double.Parse(sizes[index].Groups[1].ValueSpan, CultureInfo.InvariantCulture);
            var height = double.Parse(sizes[index].Groups[2].ValueSpan, CultureInfo.InvariantCulture);
            var rotation = int.Parse(rotations[index].Groups[1].ValueSpan, CultureInfo.InvariantCulture);
            pages[index] = rotation % 180 == 0 ? new(width, height) : new(height, width);
        }

        return (count, pages);
    }

    private sealed record PageSize(double Width, double Height);

    private static PdfRequestException TooManyPages() => new(422, "The PDF page limit was exceeded.");

    [GeneratedRegex(@"^Pages:[ \t]+([0-9]+)", RegexOptions.Multiline)]
    private static partial Regex PageCount();

    [GeneratedRegex(@"^Encrypted:[ \t]+yes\b", RegexOptions.Multiline)]
    private static partial Regex Encrypted();

    [GeneratedRegex(@"^Page[ \t]+[0-9]+ size:[ \t]+([0-9.]+) x ([0-9.]+) pts", RegexOptions.Multiline)]
    private static partial Regex PageDimensions();

    [GeneratedRegex(@"^Page[ \t]+[0-9]+ rot:[ \t]+(-?[0-9]+)", RegexOptions.Multiline)]
    private static partial Regex PageRotation();
}

using System.Text.Json;
using Microsoft.AspNetCore.WebUtilities;
using Microsoft.Extensions.Options;
using Wolverine.Http;

namespace OssPdf;

public static class PdfEndpoints
{
    [WolverinePost("/HtmlToPdf")]
    public static Task<IResult> HtmlToPdf(HttpContext context, PdfJobs jobs, HtmlRenderer renderer, PdfConverters converters) =>
        jobs.RunAsync(context, PdfEngine.Html, async (job, cancellationToken) =>
        {
            if (!context.Request.HasJsonContentType())
            {
                throw new PdfRequestException(415, "An application/json request is required.");
            }

            HtmlToPdfRequest? request;
            try
            {
                request = await context.Request.ReadFromJsonAsync<HtmlToPdfRequest>(cancellationToken);
            }
            catch (JsonException)
            {
                throw new PdfRequestException(400, "Invalid HTML conversion request.");
            }

            if (request is null)
            {
                throw new PdfRequestException(400, "An HTML conversion request is required.");
            }

            var name = DownloadName(request.FileName);
            await renderer.RenderAsync(request, job, cancellationToken);
            await converters.CheckGeneratedPdfAsync(job, cancellationToken);
            return name;
        });

    [WolverinePost("/WordToPdf")]
    public static Task<IResult> WordToPdf(
        MultipartReader reader, HttpContext context, PdfJobs jobs, PdfConverters converters) =>
        jobs.RunAsync(context, PdfEngine.Word, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Word, false, 1, cancellationToken);
            await converters.WordToPdfAsync(job, upload.Paths[0], cancellationToken);
            await converters.CheckGeneratedPdfAsync(job, cancellationToken);
            return "document.pdf";
        });

    [WolverinePost("/Compress")]
    public static Task<IResult> Compress(
        MultipartReader reader, HttpContext context, PdfJobs jobs, PdfConverters converters) =>
        jobs.RunAsync(context, PdfEngine.Pdf, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Pdf, true, 1, cancellationToken);
            await converters.CompressAsync(job, upload.Paths[0], upload.Quality!.Value, cancellationToken);
            return "compressed.pdf";
        });

    [WolverinePost("/Merge")]
    public static Task<IResult> Merge(
        MultipartReader reader, HttpContext context, PdfJobs jobs, PdfConverters converters, IOptions<PdfLimits> options) =>
        jobs.RunAsync(context, PdfEngine.Pdf, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Pdf, false, options.Value.MaxMergeFiles, cancellationToken);
            await converters.MergeAsync(job, upload.Paths, cancellationToken);
            return "merged.pdf";
        });

    [WolverinePost("/ImageToPdf")]
    public static Task<IResult> ImageToPdf(
        MultipartReader reader, HttpContext context, PdfJobs jobs, PdfConverters converters) =>
        jobs.RunAsync(context, PdfEngine.Raster, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Image, false, 1, cancellationToken);
            await converters.ImageToPdfAsync(job, upload.Paths[0], upload.ImagePdfTargetBytes, cancellationToken);
            return "image.pdf";
        });

    private static string DownloadName(string? name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return "document.pdf";
        }

        if (name.Length > 132 || name.Any(character => char.IsControl(character) || character is '/' or '\\'))
        {
            throw new PdfRequestException(400, "The PDF filename is invalid.");
        }

        name = name.Trim();
        return name.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase) ? name : name + ".pdf";
    }
}

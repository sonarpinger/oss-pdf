using System.Text.Json;
using Microsoft.Extensions.Options;

namespace OssPdf;

public static class PdfEndpoints
{
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

            var name = PdfFileName.Normalize(request.FileName) ?? "document.pdf";
            await renderer.RenderAsync(request, job, cancellationToken);
            await converters.CheckGeneratedPdfAsync(job, cancellationToken);
            return name;
        });

    public static Task<IResult> WordToPdf(
        HttpContext context, PdfJobs jobs, PdfConverters converters)
    {
        var reader = Uploads.OpenMultipart(context.Request);
        return jobs.RunAsync(context, PdfEngine.Word, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Word, false, 1, cancellationToken);
            await converters.WordToPdfAsync(job, upload.Paths[0], cancellationToken);
            await converters.CheckGeneratedPdfAsync(job, cancellationToken);
            return upload.FileName ?? "document.pdf";
        });
    }

    public static Task<IResult> Compress(
        HttpContext context, PdfJobs jobs, PdfConverters converters)
    {
        var reader = Uploads.OpenMultipart(context.Request);
        return jobs.RunAsync(context, PdfEngine.Pdf, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Pdf, true, 1, cancellationToken);
            await converters.CompressAsync(job, upload.Paths[0], upload.Quality!.Value, cancellationToken);
            return upload.FileName ?? "compressed.pdf";
        });
    }

    public static Task<IResult> Merge(
        HttpContext context, PdfJobs jobs, PdfConverters converters, IOptions<PdfLimits> options)
    {
        var reader = Uploads.OpenMultipart(context.Request);
        return jobs.RunAsync(context, PdfEngine.Pdf, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Pdf, false, options.Value.MaxMergeFiles, cancellationToken);
            await converters.MergeAsync(job, upload.Paths, cancellationToken);
            return upload.FileName ?? "merged.pdf";
        });
    }

    public static Task<IResult> ImageToPdf(
        HttpContext context, PdfJobs jobs, PdfConverters converters)
    {
        var reader = Uploads.OpenMultipart(context.Request);
        return jobs.RunAsync(context, PdfEngine.Raster, async (job, cancellationToken) =>
        {
            var upload = await Uploads.ReadAsync(reader, job, UploadKind.Image, false, 1, cancellationToken);
            await converters.ImageToPdfAsync(job, upload.Paths[0], upload.ImagePdfTargetBytes, cancellationToken);
            return upload.FileName ?? "image.pdf";
        });
    }
}

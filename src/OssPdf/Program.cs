using OssPdf;

if (args is ["--install-chromium"])
{
    return Microsoft.Playwright.Program.Main(["install", "--with-deps", "--only-shell", "chromium"]);
}

if (args is ["--ironpress-worker"])
{
    IronpressWorker.Run();
    return 0;
}

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.ConfigureKestrel(options => options.Limits.MaxRequestBodySize = PdfLimits.RequestBytes);
builder.Services.AddProblemDetails();
builder.Services.AddOptions<PdfLimits>()
    .BindConfiguration("Pdf")
    .Validate(limits => limits.IsValid(), "Invalid PDF processing limits.")
    .ValidateOnStart();
builder.Services.AddSingleton<PdfJobs>();
builder.Services.AddSingleton<PdfConverters>();
builder.Services.AddSingleton<IronpressWorker>();
builder.Services.AddSingleton<HtmlRenderer>();

var app = builder.Build();
app.UseExceptionHandler();
app.Use(async (context, next) =>
{
    try
    {
        if (context.Request.ContentLength > PdfLimits.RequestBytes)
        {
            throw new PdfRequestException(413, "The request exceeds 60 MB.");
        }

        await next(context);
    }
    catch (BadHttpRequestException exception) when (!context.Response.HasStarted)
    {
        var title = exception.StatusCode == 413 ? "The request exceeds 60 MB." : "Invalid request.";
        await Results.Problem(statusCode: exception.StatusCode, title: title).ExecuteAsync(context);
    }
    catch (PdfRequestException exception) when (!context.Response.HasStarted)
    {
        await Results.Problem(statusCode: exception.StatusCode, title: exception.Message).ExecuteAsync(context);
    }
});
app.MapPost("/HtmlToPdf", PdfEndpoints.HtmlToPdf);
app.MapPost("/WordToPdf", PdfEndpoints.WordToPdf);
app.MapPost("/Compress", PdfEndpoints.Compress);
app.MapPost("/Merge", PdfEndpoints.Merge);
app.MapPost("/ImageToPdf", PdfEndpoints.ImageToPdf);
await app.RunAsync();
return 0;

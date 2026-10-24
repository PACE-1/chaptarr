using System;
using System.Linq;
using System.Net;
using System.Threading.Tasks;
using FluentValidation.Results;
using NLog;
using NzbDrone.Common.Http;
using NzbDrone.Core.Configuration;
using NzbDrone.Core.Parser;

namespace NzbDrone.Core.Indexers.Shelfmark
{
    public class Shelfmark : HttpIndexerBase<ShelfmarkIndexerSettings>
    {
        public override string Name => "Shelfmark";

        public override DownloadProtocol Protocol => DownloadProtocol.DirectDownload;
        public override bool SupportsRss => false;

        // Shelfmark already paces its own sources; a search is one request per content type and source.
        public override TimeSpan RateLimit => TimeSpan.FromSeconds(1);

        public Shelfmark(IIndexerHttpClientFactory httpClientFactory, IIndexerStatusService indexerStatusService, IConfigService configService, IParsingService parsingService, Logger logger)
            : base(httpClientFactory, indexerStatusService, configService, parsingService, logger)
        {
        }

        public override IIndexerRequestGenerator GetRequestGenerator()
        {
            return new ShelfmarkRequestGenerator { Settings = Settings };
        }

        public override IParseIndexerResponse GetParser()
        {
            return new ShelfmarkParser { Settings = Settings };
        }

        protected override async Task<IndexerResponse> FetchIndexerResponse(IndexerRequest request)
        {
            // Shelfmark answers failed searches with a JSON error body; let the parser read it instead of throwing.
            request.HttpRequest.SuppressHttpError = true;

            return await base.FetchIndexerResponse(request);
        }

        protected override async Task<ValidationFailure> TestConnection()
        {
            try
            {
                var request = new ShelfmarkRequestGenerator { Settings = Settings }.BuildTestRequest().First();
                request.HttpRequest.SuppressHttpError = true;

                var response = await FetchIndexerResponse(request);

                if (response.HttpResponse.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
                {
                    return new ValidationFailure("ApiKey", "Shelfmark rejected the API key. Use SHELFMARK_API_KEY, not the read-only key.");
                }

                if (response.HttpResponse.StatusCode != HttpStatusCode.OK)
                {
                    return new ValidationFailure("BaseUrl", $"Shelfmark answered {(int)response.HttpResponse.StatusCode}. Check the URL.");
                }
            }
            catch (Exception ex)
            {
                _logger.Warn(ex, "Unable to connect to Shelfmark");

                return new ValidationFailure("BaseUrl", "Unable to connect to Shelfmark: " + ex.Message);
            }

            return null;
        }
    }
}

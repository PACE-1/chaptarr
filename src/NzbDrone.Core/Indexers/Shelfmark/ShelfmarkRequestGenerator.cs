using System;
using System.Collections.Generic;
using System.Linq;
using NzbDrone.Common.Extensions;
using NzbDrone.Common.Http;
using NzbDrone.Core.Books;
using NzbDrone.Core.IndexerSearch.Definitions;

namespace NzbDrone.Core.Indexers.Shelfmark
{
    public class ShelfmarkRequestGenerator : IIndexerRequestGenerator
    {
        // Shelfmark searches are book-scoped and slow, so an author search covers at most this many books.
        public const int MaxBooksPerAuthorSearch = 25;

        public ShelfmarkIndexerSettings Settings { get; set; }

        public virtual IndexerPageableRequestChain GetRecentRequests()
        {
            // Shelfmark has no feed of new releases; it only answers searches for a book.
            return new IndexerPageableRequestChain();
        }

        public virtual IndexerPageableRequestChain GetSearchRequests(BookSearchCriteria searchCriteria)
        {
            var chain = new IndexerPageableRequestChain();
            var book = searchCriteria.Books?.FirstOrDefault();
            var title = searchCriteria.BookTitle.IsNotNullOrWhiteSpace() ? searchCriteria.BookTitle : book?.Title;

            chain.Add(GetBookRequests(searchCriteria.Author?.Name, title, book, SearchMediaTypeHelper.GetRequestedMediaType(searchCriteria)));

            return chain;
        }

        public virtual IndexerPageableRequestChain GetSearchRequests(AuthorSearchCriteria searchCriteria)
        {
            var chain = new IndexerPageableRequestChain();
            var books = (searchCriteria.Books ?? new List<Book>())
                .Where(b => b != null && b.Title.IsNotNullOrWhiteSpace())
                .Take(MaxBooksPerAuthorSearch)
                .ToList();

            chain.Add(books.SelectMany(b => GetBookRequests(searchCriteria.Author?.Name, b.Title, b, b.MediaType)).ToList());

            return chain;
        }

        public IEnumerable<ShelfmarkIndexerRequest> BuildTestRequest()
        {
            var request = BuildRequest("/api/release-sources");
            yield return new ShelfmarkIndexerRequest(request.Build(), null, null);
        }

        private IEnumerable<IndexerRequest> GetBookRequests(string author, string title, Book book, BookMediaType? mediaType)
        {
            if (title.IsNullOrWhiteSpace())
            {
                yield break;
            }

            var contentTypes = mediaType.HasValue
                ? new[] { ToContentType(mediaType.Value) }
                : new[] { "ebook", "audiobook" };

            foreach (var contentType in contentTypes)
            {
                var sources = Settings.GetSources();

                if (sources.Length == 0)
                {
                    yield return BuildSearchRequest(author, title, book, contentType, null);
                    continue;
                }

                foreach (var source in sources)
                {
                    yield return BuildSearchRequest(author, title, book, contentType, source);
                }
            }
        }

        private IndexerRequest BuildSearchRequest(string author, string title, Book book, string contentType, string source)
        {
            var builder = BuildRequest("/api/releases");

            if (Settings.UseHardcoverIds && book?.HardcoverBookId.IsNotNullOrWhiteSpace() == true)
            {
                builder.AddQueryParam("provider", "hardcover")
                       .AddQueryParam("book_id", book.HardcoverBookId);
            }
            else
            {
                // Shelfmark's "manual" provider searches the sources by title and author without a metadata lookup.
                builder.AddQueryParam("provider", "manual")
                       .AddQueryParam("book_id", book != null && book.Id > 0 ? "chaptarr-" + book.Id : "chaptarr");
            }

            builder.AddQueryParam("title", title)
                   .AddQueryParam("content_type", contentType);

            if (author.IsNotNullOrWhiteSpace())
            {
                builder.AddQueryParam("author", author);
            }

            if (source.IsNotNullOrWhiteSpace())
            {
                builder.AddQueryParam("source", source);
            }

            var request = builder.Build();
            request.RequestTimeout = TimeSpan.FromSeconds(Settings.SearchTimeout);

            return new ShelfmarkIndexerRequest(request, author, contentType);
        }

        private HttpRequestBuilder BuildRequest(string resource)
        {
            var builder = new HttpRequestBuilder(Settings.BaseUrl.TrimEnd('/'))
                .Resource(resource)
                .Accept(HttpAccept.Json)
                .SetHeader("X-Api-Key", Settings.ApiKey);

            return builder;
        }

        public static string ToContentType(BookMediaType mediaType)
        {
            return mediaType == BookMediaType.Ebook ? "ebook" : "audiobook";
        }
    }

    public class ShelfmarkIndexerRequest : IndexerRequest
    {
        public ShelfmarkIndexerRequest(HttpRequest httpRequest, string author, string contentType)
            : base(httpRequest)
        {
            Author = author;
            ContentType = contentType;
        }

        public string Author { get; }
        public string ContentType { get; }
    }
}

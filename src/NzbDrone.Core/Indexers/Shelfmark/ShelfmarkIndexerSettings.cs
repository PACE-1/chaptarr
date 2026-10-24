using FluentValidation;
using NzbDrone.Common.Extensions;
using NzbDrone.Core.Annotations;
using NzbDrone.Core.Validation;

namespace NzbDrone.Core.Indexers.Shelfmark
{
    public class ShelfmarkIndexerSettingsValidator : AbstractValidator<ShelfmarkIndexerSettings>
    {
        public ShelfmarkIndexerSettingsValidator()
        {
            RuleFor(c => c.BaseUrl).ValidRootUrl();
            RuleFor(c => c.ApiKey).NotEmpty();
            RuleFor(c => c.SearchTimeout).InclusiveBetween(10, 600);
        }
    }

    public class ShelfmarkIndexerSettings : IIndexerSettings
    {
        private static readonly ShelfmarkIndexerSettingsValidator Validator = new ShelfmarkIndexerSettingsValidator();

        public ShelfmarkIndexerSettings()
        {
            BaseUrl = "http://shelfmark:8084";
            SearchTimeout = 120;
        }

        [FieldDefinition(0, Label = "URL", HelpText = "Shelfmark's address as Chaptarr reaches it, e.g. http://shelfmark:8084")]
        public string BaseUrl { get; set; }

        [FieldDefinition(1, Label = "API Key", Privacy = PrivacyLevel.ApiKey, HelpText = "Shelfmark's SHELFMARK_API_KEY (the admin key, not the read-only one)")]
        public string ApiKey { get; set; }

        [FieldDefinition(2, Label = "Sources", HelpText = "Shelfmark release sources to search, e.g. direct_download, prowlarr, irc. Leave empty to search every source enabled in Shelfmark.", Advanced = true)]
        public string Sources { get; set; }

        [FieldDefinition(3, Type = FieldType.Checkbox, Label = "Use Hardcover IDs", HelpText = "Search by the book's Hardcover ID when it has one. Needs Hardcover set up as a metadata provider in Shelfmark. Books without one are searched by title and author.", Advanced = true)]
        public bool UseHardcoverIds { get; set; }

        [FieldDefinition(4, Type = FieldType.Number, Label = "Search Timeout", Unit = "seconds", HelpText = "How long one Shelfmark search may take. Match or exceed Shelfmark's own release search timeout.", Advanced = true)]
        public int SearchTimeout { get; set; }

        [FieldDefinition(5, Type = FieldType.Number, Label = "Early Download Limit", Unit = "days", HelpText = "Time before release date Chaptarr will download from this indexer, empty is no limit", Advanced = true)]
        public int? EarlyReleaseLimit { get; set; }

        public string[] GetSources()
        {
            return Sources.IsNullOrWhiteSpace()
                ? System.Array.Empty<string>()
                : Sources.Split(new[] { ',', ' ', ';' }, System.StringSplitOptions.RemoveEmptyEntries);
        }

        public NzbDroneValidationResult Validate()
        {
            return new NzbDroneValidationResult(Validator.Validate(this));
        }
    }
}

using FluentValidation;
using NzbDrone.Common.Extensions;
using NzbDrone.Core.Annotations;
using NzbDrone.Core.ThingiProvider;
using NzbDrone.Core.Validation;

namespace NzbDrone.Core.Download.Clients.Shelfmark
{
    public class ShelfmarkDownloadClientSettingsValidator : AbstractValidator<ShelfmarkDownloadClientSettings>
    {
        public ShelfmarkDownloadClientSettingsValidator()
        {
            RuleFor(c => c.Host).ValidHost();
            RuleFor(c => c.Port).InclusiveBetween(1, 65535);
            RuleFor(c => c.UrlBase).ValidUrlBase().When(c => c.UrlBase.IsNotNullOrWhiteSpace());
            RuleFor(c => c.ApiKey).NotEmpty();
            RuleFor(c => c.UserId).GreaterThan(0).When(c => c.UserId.HasValue);
        }
    }

    public class ShelfmarkDownloadClientSettings : IProviderConfig
    {
        private static readonly ShelfmarkDownloadClientSettingsValidator Validator = new ShelfmarkDownloadClientSettingsValidator();

        public ShelfmarkDownloadClientSettings()
        {
            Host = "shelfmark";
            Port = 8084;
        }

        [FieldDefinition(0, Label = "Host", Type = FieldType.Textbox)]
        public string Host { get; set; }

        [FieldDefinition(1, Label = "Port", Type = FieldType.Textbox)]
        public int Port { get; set; }

        [FieldDefinition(2, Label = "Use SSL", Type = FieldType.Checkbox, HelpText = "Use a secure connection when connecting to Shelfmark")]
        public bool UseSsl { get; set; }

        [FieldDefinition(3, Label = "Url Base", Type = FieldType.Textbox, Advanced = true, HelpText = "Adds a prefix to the Shelfmark url, e.g. http://[host]:[port]/[urlBase]/api")]
        public string UrlBase { get; set; }

        [FieldDefinition(4, Label = "API Key", Type = FieldType.Textbox, Privacy = PrivacyLevel.ApiKey, HelpText = "Shelfmark's SHELFMARK_API_KEY (the admin key, not the read-only one)")]
        public string ApiKey { get; set; }

        [FieldDefinition(5, Label = "Shelfmark User ID", Type = FieldType.Number, HelpText = "Download as this Shelfmark user, so the files land in that user's destination folders. Leave empty to download as Shelfmark's admin. Only this user's downloads are shown in Chaptarr's queue.")]
        public int? UserId { get; set; }

        [FieldDefinition(6, Label = "Priority", Type = FieldType.Number, Advanced = true, HelpText = "Shelfmark queue priority for Chaptarr's downloads. Lower numbers download first.")]
        public int Priority { get; set; }

        public NzbDroneValidationResult Validate()
        {
            return new NzbDroneValidationResult(Validator.Validate(this));
        }
    }
}

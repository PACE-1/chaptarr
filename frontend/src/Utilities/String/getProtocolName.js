import titleCase from 'Utilities/String/titleCase';
import translate from 'Utilities/String/translate';

// Display name for a download protocol (usenet, torrent, directDownload).
function getProtocolName(protocol) {
  switch (protocol) {
    case 'usenet':
      return translate('Usenet');
    case 'torrent':
      return translate('Torrent');
    case 'directDownload':
      return translate('DirectDownload');
    default:
      return titleCase(protocol);
  }
}

export default getProtocolName;

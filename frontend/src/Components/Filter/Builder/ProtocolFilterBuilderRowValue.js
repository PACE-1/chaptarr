import React from 'react';
import FilterBuilderRowValue from './FilterBuilderRowValue';

const protocols = [
  { id: 'torrent', name: 'Torrent' },
  { id: 'usenet', name: 'Usenet' },
  { id: 'directDownload', name: 'Direct Download' }
];

function ProtocolFilterBuilderRowValue(props) {
  return (
    <FilterBuilderRowValue
      tagList={protocols}
      {...props}
    />
  );
}

export default ProtocolFilterBuilderRowValue;

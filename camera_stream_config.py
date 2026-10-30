"""Merge local live-stream paths without replacing existing hosts or mic settings."""
def render(text):
    import yaml
    data=yaml.safe_load(text)
    if not isinstance(data,dict) or not isinstance(data.get('paths'),dict):
        raise ValueError('MediaMTX 設定結構不正確')
    for name in ('live','live-hls'):
        previous=data['paths'].get(name)
        if previous is not None and (not isinstance(previous,dict) or previous.get('source','publisher')!='publisher'):
            raise ValueError('音畫串流名稱已被其他來源使用：'+name)
        data['paths'][name]={**(previous or {}),'source':'publisher'}
    data.update(hls=True,hlsAddress='127.0.0.1:8888',hlsVariant='lowLatency',hlsAlwaysRemux=True,moq=False,
                hlsSegmentCount=7,hlsSegmentDuration='1s',hlsPartDuration='200ms',hlsSegmentMaxSize='10M')
    return yaml.safe_dump(data,sort_keys=False,allow_unicode=True).encode()

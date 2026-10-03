#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} =~ ^[a-z0-9]{20,40}$ ]] || exit 2
helper=sha256:c7a7748b233da32a7541213bd6c124d104d2221ac7d9020976dbfa02ba88e57d
docker run --rm -i --pull=never --network none --entrypoint bash \
  --mount type=bind,source=/opt/retired-deployments,target=/recovery \
  --mount type=bind,source=/run/ashbi-docker-capacity,target=/lock \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  "$helper" -s -- "$1" <<'HOST'
set -euo pipefail
umask 077
exec 9>/lock/maintenance.lock
flock -x 9
mapfile -t ids < <(docker ps -q --filter "label=com.docker.compose.project=$1" --filter label=com.docker.compose.service=photogen)
[[ ${#ids[@]} -le 1 ]] || exit 1
container=${ids[0]:-photogen}
[[ $(docker inspect -f '{{.State.Running}}' "$container") == true ]] || exit 1
data=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' "$container")
outputs=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/outputs"}}{{.Source}}{{end}}{{end}}' "$container")
[[ $data == /root/photogen-data || $data == /opt/photogen-coolify/data ]] || exit 1
[[ $outputs == /root/photogen-outputs || $outputs == /opt/photogen-coolify/outputs ]] || exit 1
size=$(docker exec "$container" du -sk /app/data /app/outputs | awk '{total+=$1} END {print total}')
[[ $size =~ ^[0-9]+$ ]] || exit 1
[[ $(df -Pk /recovery | awk 'NR==2 {print $4}') -gt $((1048576 + 2 * size)) ]] || exit 1
docker volume ls -q | LC_ALL=C sort > /recovery/.photogen-volumes-$1-before
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
name="photogen-helper-predeploy-$stamp"
folder="/recovery/$name"
mkdir -m 700 "$folder"
docker inspect "$container" > "$folder/container-inspect.private.json"
image=$(docker inspect -f '{{.Image}}' "$container")
tag="ashbi-recovery/photogen-helper:${stamp,,}"
source="ashbi-recovery/photogen-helper-source:${stamp,,}"
docker tag "$image" "$source"
printf 'FROM %s\nLABEL coolify.managed="true" ashbi.recovery="photogen"\n' "$source" |
  docker build --pull=false --network=none -t "$tag" - > "$folder/image-retention.private.log" 2>&1
before_layers=$(docker image inspect -f '{{json .RootFS.Layers}}' "$image")
after_layers=$(docker image inspect -f '{{json .RootFS.Layers}}' "$tag")
[[ "$before_layers" == "$after_layers" ]]
retained=$(docker image inspect -f '{{.Id}}' "$tag")
docker run --rm -i --pull=never --network none --read-only --user 0:0 \
  --entrypoint /usr/local/bin/python \
  --mount "type=bind,source=$data,target=/app/data,readonly" \
  --mount "type=bind,source=$outputs,target=/app/outputs,readonly" \
  --mount "type=bind,source=/opt/retired-deployments/$name,target=/private" \
  "$image" - "$image" "$retained" "$tag" "$name" <<'PY'
import hashlib,json,os,pathlib,shutil,sqlite3,sys
os.umask(0o077)
base=pathlib.Path('/private'); snapshot=base/'snapshot'; snapshot.mkdir(mode=0o700)
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def tables(c):
    result={}
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows=[]
        for row in c.execute('SELECT * FROM "'+name.replace('"','""')+'"'):
            data=[{'blob':v.hex()} if isinstance(v,bytes) else v for v in row]
            rows.append(hashlib.sha256(json.dumps(data,separators=(',',':'),ensure_ascii=True).encode()).hexdigest())
        result[name]={'rows':len(rows),'sha256':hashlib.sha256(''.join(sorted(rows)).encode()).hexdigest()}
    return result
files={}; databases=[]; regular={}
for label in ['data','outputs']:
    root=pathlib.Path('/app')/label; (snapshot/label).mkdir(mode=0o700)
    for p in sorted(root.rglob('*')):
        assert not p.is_symlink()
        if p.is_dir():continue
        assert p.is_file()
        if p.name.endswith(('-wal','-shm','-journal')):continue
        rel=p.relative_to(root); dest=snapshot/label/rel; dest.parent.mkdir(parents=True,exist_ok=True)
        if p.suffix.lower() in ['.db','.sqlite','.sqlite3']:
            c=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True);c.execute('BEGIN');c.execute('SELECT count(*) FROM sqlite_master').fetchone(); expected=tables(c)
            target=sqlite3.connect(dest);c.backup(target);target.execute('PRAGMA journal_mode=DELETE')
            assert target.execute('PRAGMA integrity_check').fetchone()==('ok',) and tables(target)==expected
            target.close();c.close();databases.append({'path':label+'/'+str(rel),'tables':expected})
        else:
            before=sha(p);shutil.copyfile(p,dest);assert sha(dest)==before==sha(p);regular[str(p)]=before
        files[label+'/'+str(rel)]={'bytes':dest.stat().st_size,'sha256':sha(dest)}
restore=base/'independent-restore';shutil.copytree(snapshot,restore)
assert {str(p.relative_to(restore)):{'bytes':p.stat().st_size,'sha256':sha(p)} for p in restore.rglob('*') if p.is_file()}==files
for db in databases:
    c=sqlite3.connect('file:'+str(restore/db['path'])+'?mode=ro',uri=True)
    assert c.execute('PRAGMA integrity_check').fetchone()==('ok',) and tables(c)==db['tables'];c.close()
assert all(sha(pathlib.Path(p))==v for p,v in regular.items())
(base/'storage-manifest.private.json').write_text(json.dumps(files))
(base/'database-tables.private.json').write_text(json.dumps(databases))
proof={'backupDirectory':'/opt/retired-deployments/'+sys.argv[4],'fileCount':len(files),'sqliteDatabases':len(databases),'restoredTableHashesMatch':True,'independentRestoreFilesMatch':True,'sourceImage':sys.argv[1],'protectedImage':sys.argv[2],'protectedTag':sys.argv[3],'rootFSLayersMatch':True,'liveContainerStopped':False}
(base/'recovery-proof.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
PY
[[ $(docker inspect -f '{{.State.Running}}' "$container") == true ]] || exit 1
docker volume ls -q | LC_ALL=C sort > /recovery/.photogen-volumes-$1-after
cmp /recovery/.photogen-volumes-$1-before /recovery/.photogen-volumes-$1-after
HOST

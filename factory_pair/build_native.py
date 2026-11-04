"""Build a pinned libmlink adapter DLL; does not access BLE/devices."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk',type=Path,default=Path('E:/onm/SC201_master_omn'))
    parser.add_argument('--compiler',default='gcc')
    args=parser.parse_args()
    base=Path(__file__).resolve().parent / 'native'
    lib=args.sdk/'components/libmlink'
    sources=[base/'bridge.c']+[lib/'src'/n for n in ('link.c','wire.c','ctrl.c','json.c')]+[lib/'third_party/cjson/cJSON.c']
    target=base/'factory_mlink.dll'
    command=[args.compiler,'-std=c11','-Wall','-Wextra','-Werror','-O2','-shared','-static-libgcc',
             '-I'+str(lib/'include'),'-I'+str(lib/'third_party/cjson')]+[str(p) for p in sources]+['-o',str(target)]
    subprocess.run(command,check=True)
    sha=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    inputs=sources+list((lib/'include/mlink').glob('*.h'))+[lib/'third_party/cjson/cJSON.h']
    report={'sdk':str(args.sdk.resolve()),'sources':{str(p):sha(p) for p in inputs},
            'library_modified':False,'dll_sha256':sha(target),'body_limit':512,
            'compiler':subprocess.check_output([args.compiler,'--version'],text=True).splitlines()[0]}
    (base/'build.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('Built '+str(target))


if __name__=='__main__': main()

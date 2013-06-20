# kaleido

A multi-way file synchronizer using git as transport

Copyright (C) 2011,2013 Hyeontaek Lim

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.


## Installation

> $ git clone git@github.com:hyeontaek/kaleido.git
* (Add the kaleido directory to PATH)
* Requires: Python 3
* Requires: inotify-tools (for Linux), pywin32 (for Windows)
* Caution: MacOS X and other platforms are not tested; the local file change monitor will not work (not implemented)


## Basic usage

### Initialization

* On Machine A,
> $ mkdir ~/sync<br/>
> $ cd ~/sync<br/>
> $ kaleido init<br/>

* On Machine B, C, ... (new machines can be added later),
> $ mkdir ~/sync<br/>
> $ cd ~/sync<br/>
> $ kaleido clone USER@MachineA:~/sync<br/>

### Synchronization

* Changes on any machine will be propagated to other machines
* Conflicts are resolved silently; manually fix them by editing the files or checking out one version (see executing any custom git command)

* On Machine A,
> $ kaleido -B -b 0.0.0.0:50000 sync-forever &<br/>

* On other machines,
> $ kaleido -b MachineA:50000 sync-forever &<br/>

### Miscellaneous

* To exclude some files,
  * Use .gitignore; or
  * List them in .kaleido-ignore at the root of the sync

* To include git repositories for synchronization,
> $ kaleido track-git PATH<br/>

* To exclude git repositories from synchronization,
> $ kaleido untrack-git PATH<br/>

* When .kaleido directory becomes too big, on Machine A,
> $ kaleido -b 127.0.0.1:50000 -D squash<br/>
  * All other machines synchronizing will also compact .kaleido directory

* To execute any custom git command (e.g., to checkout old files),
> $ kaleido GIT-COMMAND ARGUMENTS ...<br/>


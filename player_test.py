#!/usr/bin/env python
# vim: sw=4:ts=4:sts=4:fdm=indent:fdl=0:
# -*- coding: UTF8 -*-
#
# Test the player object.
# Copyright (C) 2012 Josiah Gordon <josiahg@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


""" Test the player object.

"""

def main(args: dict) -> None:
    """ Play args['filename'] args['loops'] times.

    """

    from sys import stdin as sys_stdin
    from select import select
    from time import sleep as time_sleep

    from musio.player_util import AudioPlayer

    player = AudioPlayer(show_position=True, **args)

    player.loops = args['loops']

    print("Playing: %(filename)s" % args)
    print(player)

    player.play()

    while player.playing:
        # Check for input.
        r, _, _ = select([sys_stdin], [], [], 0)

        # Get input if there was any otherwise continue.
        if r:
            command = r[0].readline().strip().lower()
        else:
            time_sleep(0.1)
            continue

        # Handle input commands.
        if command.startswith('p'):
            player.play() if player.paused else player.pause()
        elif not command:
            break

    print("\nDone.")

    player.stop()


if __name__ == '__main__':
    from argparse import ArgumentParser
    parser = ArgumentParser(description="Player test")
    parser.add_argument('-l', '--loops', action='store', default=-1, type=int,
                        help='How many times to loop (-1 = infinite)',
                        dest='loops')
    parser.add_argument('-t', '--track', action='store', default=0, type=int,
                        help='Track to play', dest='track')
    parser.add_argument('-s', '--soundfont', action='store',
                        default='/usr/share/soundfonts/fluidr3/FluidR3GM.SF2',
                        help='Soundfont to use when playing midis',
                        dest='soundfont')
    parser.add_argument(dest='filename')
    args = parser.parse_args()

    if args.filename:
        main(args.__dict__)

// Windows 10+ / Windows PowerShell 5.1. No service or external process wrapper.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.IO;
using System.IO.Pipes;
using System.Threading.Tasks;

namespace Csd.Windows {
    public sealed class ContainedProcess : IDisposable {
        [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinWorkingSet, MaxWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)] struct IoCounters {
            public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct StartupInfo {
            public uint Size;
            public IntPtr Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
            public ushort ShowWindow, ReservedSize;
            public IntPtr ReservedBytes, Stdin, Stdout, Stderr;
        }
        [StructLayout(LayoutKind.Sequential)] struct StartupInfoEx {
            public StartupInfo Startup;
            public IntPtr Attributes;
        }
        [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
            public IntPtr Process, Thread;
            public uint ProcessId, ThreadId;
        }
        [StructLayout(LayoutKind.Sequential)] struct Accounting {
            public long UserTime, KernelTime, PeriodUserTime, PeriodKernelTime;
            public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
        }
        [UnmanagedFunctionPointer(CallingConvention.Winapi)] delegate bool ConsoleHandler(uint signal);
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateJobObjectW(IntPtr security, IntPtr name);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits limits, uint length);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory, ref StartupInfoEx startup, out ProcessInfo process);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateJobObject(IntPtr job, uint code);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool QueryInformationJobObject(IntPtr job, int kind, ref Accounting accounting, uint length, IntPtr returned);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetConsoleCtrlHandler(ConsoleHandler handler, bool add);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint GetConsoleProcessList([Out] uint[] processes, uint count);
        [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GetStdHandle(int kind);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr source, IntPtr targetProcess, out IntPtr target, uint access, bool inherit, uint options);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
        IntPtr job, process;
        public uint Id { get; private set; }
        AnonymousPipeServerStream outputPipe, errorPipe, inputPipe;
        Task<string> outputTask, errorTask;
        Task inputTask;
        int capturedCharacters, outputExceeded;
        int maximumOutput;
        public bool OutputExceeded { get { return Interlocked.CompareExchange(ref outputExceeded, 0, 0) != 0; } }
        public bool OutputComplete { get { return outputTask != null && outputTask.IsCompleted && errorTask.IsCompleted && (inputTask == null || inputTask.IsCompleted); } }
        public string Output { get { return outputTask.Result; } }
        public string ErrorOutput { get { return errorTask.Result; } }

        string ReadBounded(Stream pipe) {
            var result = new StringBuilder();
            using (var reader = new StreamReader(pipe, new UTF8Encoding(false, false), true, 1024)) {
                var buffer = new char[1024];
                int count;
                while ((count = reader.Read(buffer, 0, buffer.Length)) > 0) {
                    if (OutputExceeded) continue;
                    int total = Interlocked.Add(ref capturedCharacters, count);
                    if (total > maximumOutput) {
                        Interlocked.Exchange(ref outputExceeded, 1);
                        // Continue draining so a noisy child cannot deadlock its
                        // pipes. The supervisor observes this flag and stops it.
                        continue;
                    }
                    result.Append(buffer, 0, count);
                }
            }
            return result.ToString();
        }

        static string Quote(string value) {
            var result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char ch in value) {
                if (ch == '\\') { slashes++; continue; }
                result.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);
                result.Append(ch);
                slashes = 0;
            }
            result.Append('\\', slashes * 2);
            return result.Append('"').ToString();
        }

        public ContainedProcess(string executable, string[] arguments, string directory)
            : this(executable, arguments, directory, false) { }

        public ContainedProcess(string executable, string[] arguments, string directory, bool noWindow) {
            Start(executable, arguments, directory, noWindow, false, null, 0);
        }

        public ContainedProcess(string executable, string[] arguments, string directory,
                                string input, int outputLimit) {
            if (outputLimit < 1024 || outputLimit > 65536) throw new ArgumentOutOfRangeException("outputLimit");
            Start(executable, arguments, directory, true, true, input, outputLimit);
        }

        void Start(string executable, string[] arguments, string directory, bool noWindow,
                   bool capture, string input, int outputLimit) {
            IntPtr attributes = IntPtr.Zero, jobValue = IntPtr.Zero, handleList = IntPtr.Zero;
            var standard = new IntPtr[3];
            bool initialized = false;
            try {
                // Unnamed, non-inheritable handle: only this launcher owns lifetime.
                job = CreateJobObjectW(IntPtr.Zero, IntPtr.Zero);
                if (job == IntPtr.Zero) throw new Win32Exception();
                var limits = new ExtendedLimits();
                limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
                if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits))) throw new Win32Exception();
                IntPtr size = IntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref size);
                if (size == IntPtr.Zero) throw new Win32Exception();
                attributes = Marshal.AllocHGlobal(size);
                if (!InitializeProcThreadAttributeList(attributes, 2, 0, ref size)) throw new Win32Exception();
                initialized = true;
                jobValue = Marshal.AllocHGlobal(IntPtr.Size);
                Marshal.WriteIntPtr(jobValue, job);
                // Atomic assignment at creation: even a launcher crash cannot leave
                // a suspended, unassigned child. Requires Windows 10 or later.
                if (!UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x0002000D), jobValue, new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
                // Explicit allowlist: preserve redirected diagnostics without
                // inheriting unrelated handles (especially job/mutex ownership).
                handleList = Marshal.AllocHGlobal(IntPtr.Size * standard.Length);
                if (capture) {
                    maximumOutput = outputLimit;
                    outputPipe = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.Inheritable);
                    errorPipe = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.Inheritable);
                    if (input != null) inputPipe = new AnonymousPipeServerStream(PipeDirection.Out, HandleInheritability.Inheritable);
                }
                for (int index = 0; index < standard.Length; index++) {
                    IntPtr source = IntPtr.Zero;
                    if (capture && index == 1) source = new IntPtr(long.Parse(outputPipe.GetClientHandleAsString()));
                    if (capture && index == 2) source = new IntPtr(long.Parse(errorPipe.GetClientHandleAsString()));
                    if (capture && index == 0 && inputPipe != null) source = new IntPtr(long.Parse(inputPipe.GetClientHandleAsString()));
                    if (source != IntPtr.Zero) {
                        if (!DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(), out standard[index], 0, true, 2)) throw new Win32Exception();
                    }
                    else standard[index] = CopyStandardHandle(-10 - index, index == 0);
                    Marshal.WriteIntPtr(handleList, index * IntPtr.Size, standard[index]);
                }
                if (!UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x00020002), handleList, new IntPtr(IntPtr.Size * standard.Length), IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
                var startup = new StartupInfoEx();
                startup.Startup.Size = (uint)Marshal.SizeOf(startup);
                startup.Startup.Flags = 0x100; // STARTF_USESTDHANDLES
                startup.Startup.Stdin = standard[0];
                startup.Startup.Stdout = standard[1];
                startup.Startup.Stderr = standard[2];
                startup.Attributes = attributes;
                var command = new StringBuilder(Quote(executable));
                foreach (string argument in arguments) command.Append(" ").Append(Quote(argument));
                ProcessInfo child;
                if (!CreateProcessW(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                    0x00080000U | (noWindow ? 0x08000000U : 0), IntPtr.Zero, directory, ref startup, out child)) throw new Win32Exception();
                process = child.Process;
                Id = child.ProcessId;
                CloseHandle(child.Thread);
                if (capture) {
                    outputPipe.DisposeLocalCopyOfClientHandle();
                    errorPipe.DisposeLocalCopyOfClientHandle();
                    outputTask = Task.Run(() => ReadBounded(outputPipe));
                    errorTask = Task.Run(() => ReadBounded(errorPipe));
                    if (inputPipe != null) {
                        inputPipe.DisposeLocalCopyOfClientHandle();
                        inputTask = Task.Run(() => {
                            using (var writer = new StreamWriter(inputPipe, new UTF8Encoding(false))) {
                                writer.Write(input);
                            }
                        });
                    }
                }
            }
            catch { Dispose(); throw; }
            finally {
                if (initialized) DeleteProcThreadAttributeList(attributes);
                if (attributes != IntPtr.Zero) Marshal.FreeHGlobal(attributes);
                if (jobValue != IntPtr.Zero) Marshal.FreeHGlobal(jobValue);
                if (handleList != IntPtr.Zero) Marshal.FreeHGlobal(handleList);
                foreach (IntPtr handle in standard) if (handle != IntPtr.Zero) CloseHandle(handle);
            }
        }

        static IntPtr CopyStandardHandle(int kind, bool input) {
            IntPtr copied, current = GetCurrentProcess(), original = GetStdHandle(kind);
            if (original != IntPtr.Zero && original != new IntPtr(-1)) {
                if (DuplicateHandle(current, original, current, out copied, 0, true, 2)) return copied;
                int error = Marshal.GetLastWin32Error();
                if (error != 6) throw new Win32Exception(error);
            }
            // A boot task may have no standard streams at all. NUL is a device,
            // not a file, and avoids creating a console or private log implicitly.
            original = CreateFileW("NUL", input ? 0x80000000U : 0x40000000U, 3, IntPtr.Zero, 3, 0x80, IntPtr.Zero);
            if (original == new IntPtr(-1)) throw new Win32Exception();
            try {
                if (!DuplicateHandle(current, original, current, out copied, 0, true, 2)) throw new Win32Exception();
                return copied;
            }
            finally { CloseHandle(original); }
        }

        public bool Wait(int milliseconds) {
            uint result = WaitForSingleObject(process, (uint)milliseconds);
            if (result == 0xFFFFFFFF) throw new Win32Exception();
            return result == 0;
        }
        public int ExitCode {
            get { uint code; if (!GetExitCodeProcess(process, out code)) throw new Win32Exception(); return unchecked((int)code); }
        }
        public void Dispose() {
            try {
                // On an orderly exit, confirm the whole tree has stopped before
                // returning to the launcher (which then releases its mutex).
                if (job != IntPtr.Zero) {
                    if (!TerminateJobObject(job, 1)) throw new Win32Exception();
                    var deadline = Stopwatch.StartNew();
                    var accounting = new Accounting();
                    while (true) {
                        if (!QueryInformationJobObject(job, 1, ref accounting, (uint)Marshal.SizeOf(accounting), IntPtr.Zero)) throw new Win32Exception();
                        if (accounting.ActiveProcesses == 0) break;
                        if (deadline.Elapsed.TotalSeconds >= 5) throw new TimeoutException("Windows did not confirm shutdown of the contained process tree.");
                        Thread.Sleep(10);
                    }
                }
            }
            finally {
                // On launcher crash the kernel closes this same last handle,
                // killing descendants even when no finally block can execute.
                if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
                if (process != IntPtr.Zero) { CloseHandle(process); process = IntPtr.Zero; }
                // Terminating the entire job closes inherited pipe endpoints.
                // Readers retain bounded buffers and cannot keep children alive.
                if (inputPipe != null) { inputPipe.Dispose(); inputPipe = null; }
                if (outputPipe != null) { outputPipe.Dispose(); outputPipe = null; }
                if (errorPipe != null) { errorPipe.Dispose(); errorPipe = null; }
            }
        }
        public static int Run(string executable, string[] arguments, string directory) {
            int stopping = 0;
            ConsoleHandler handler = delegate(uint signal) {
                if (signal != 0 && signal != 1) return false;
                Interlocked.Exchange(ref stopping, 1);
                return true; // Child receives the same console event and can clean up.
            };
            // Password-logon boot tasks have no console; that does not disable
            // mandatory job containment. Only the console signal hook is optional.
            bool consoleAttached = HasAttachedConsole();
            if (consoleAttached && !SetConsoleCtrlHandler(handler, true)) throw new Win32Exception();
            try {
                using (var child = new ContainedProcess(executable, arguments, directory, !consoleAttached)) {
                    Stopwatch stopDeadline = null;
                    while (!child.Wait(100)) {
                        if (Interlocked.CompareExchange(ref stopping, 0, 0) != 0) {
                            if (stopDeadline == null) stopDeadline = Stopwatch.StartNew();
                            if (stopDeadline.Elapsed.TotalSeconds >= 15) return 0;
                        }
                    }
                    return Interlocked.CompareExchange(ref stopping, 0, 0) != 0 ? 0 : child.ExitCode;
                }
            }
            finally { if (consoleAttached) SetConsoleCtrlHandler(handler, false); GC.KeepAlive(handler); }
        }
        public static bool HasAttachedConsole() {
            if (GetConsoleProcessList(new uint[1], 1) != 0) return true;
            int error = Marshal.GetLastWin32Error();
            if (error == 6) return false; // ERROR_INVALID_HANDLE: no attached console.
            throw new Win32Exception(error);
        }
    }
}

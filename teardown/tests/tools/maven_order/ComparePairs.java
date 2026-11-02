import java.io.*;
import org.apache.maven.artifact.versioning.ComparableVersion;

// Reads "a<TAB>b" lines, prints the sign of ComparableVersion(a).compareTo(b) for each.
public class ComparePairs {
    public static void main(String[] args) throws IOException {
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
        for (String line; (line = in.readLine()) != null; ) {
            String[] p = line.split("\t", -1);
            System.out.println(Integer.signum(new ComparableVersion(p[0]).compareTo(new ComparableVersion(p[1]))));
        }
    }
}
